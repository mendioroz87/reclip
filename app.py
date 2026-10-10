import os
import uuid
import glob
import json
import subprocess
import threading
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}

# Partial downloads and sidecar files yt-dlp may leave next to the finished video.
NON_VIDEO_SUFFIXES = {
    ".part", ".ytdl", ".temp", ".tmp", ".json", ".description", ".txt",
    ".jpg", ".jpeg", ".png", ".webp", ".vtt", ".srt", ".ass", ".lrc",
}

# Seconds yt-dlp may spend downloading one video. Slow hosts need more.
DOWNLOAD_TIMEOUT = int(os.environ.get("RECLIP_DOWNLOAD_TIMEOUT", 300))
MAX_EMBEDS = 3
MAX_PAGE_BYTES = 2_000_000
PLAYER_META = {"og:video", "og:video:url", "og:video:secure_url", "twitter:player"}
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


class EmbedFinder(HTMLParser):
    """Collect the players a page embeds: og:video / twitter:player metas and iframes."""

    def __init__(self):
        super().__init__()
        self.players = []
        self.frames = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "iframe":
            self.frames.append(attrs.get("src"))
        elif tag == "meta" and (attrs.get("property") or attrs.get("name")) in PLAYER_META:
            self.players.append(attrs.get("content"))


def find_embed_urls(page_url):
    """Return http(s) player URLs embedded in a page, explicit video metas first."""
    try:
        req = urllib.request.Request(page_url, headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=15) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            html = response.read(MAX_PAGE_BYTES).decode(charset, "replace")
    except Exception:
        return []
    finder = EmbedFinder()
    finder.feed(html)
    found = []
    for raw in finder.players + finder.frames:
        absolute = urljoin(page_url, raw.strip()) if raw else None
        if (absolute and urlparse(absolute).scheme in {"http", "https"}
                and absolute != page_url and absolute not in found):
            found.append(absolute)
    return found[:MAX_EMBEDS]


def run_ytdlp(args, url, timeout):
    """Run yt-dlp on ``url``; if the page itself is unsupported, retry on the videos it embeds.

    Sites without an extractor often just wrap a player from another host in an
    iframe, which yt-dlp can extract when given that player URL and the page as Referer.
    """
    deadline = time.monotonic() + timeout

    def run(extra, target):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("yt-dlp", timeout)
        return subprocess.run(["yt-dlp", *args, *extra, target],
                              capture_output=True, text=True, timeout=remaining)

    result = run([], url)
    if result.returncode == 0 or "Unsupported URL" not in result.stderr:
        return result
    for embed in find_embed_urls(url):
        retry = run(["--referer", url], embed)
        if retry.returncode == 0:
            return retry
    return result


def safe_filename(title, extension, fallback):
    """Build a title-based filename that is safe for a download header."""
    title = " ".join((title or "").split())
    title = "".join(c for c in title if c not in r'\/:*?"<>|' and ord(c) >= 32 and ord(c) != 127)
    title = title[:100].strip(" .")
    return f"{title}{extension}" if title else fallback


def probe_media(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Could not inspect the downloaded video.")
    return json.loads(result.stdout)["streams"]


def ensure_compatible_mp4(filename):
    """Produce H.264/AAC MP4; copy streams that already meet the requirements."""
    source = Path(filename)
    streams = probe_media(source)
    video = next((s for s in streams if s["codec_type"] == "video"), None)
    audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    if not video or video.get("disposition", {}).get("attached_pic"):
        raise RuntimeError("The downloaded file has no video track.")
    copy_video = (
        video.get("codec_name") == "h264" and video.get("pix_fmt") == "yuv420p"
        and video.get("profile") in {"Baseline", "Constrained Baseline", "Main", "High"}
    )
    copy_audio = not audio or (audio.get("codec_name") == "aac" and audio.get("profile") == "LC")
    target = source.with_suffix(".mp4")
    temporary = source.with_name(source.stem + ".compatible.mp4")
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(source),
           "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy" if copy_video else "libx264"]
    if not copy_video:
        cmd += ["-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-threads", "2"]
    cmd += ["-c:a", "copy" if copy_audio else "aac"]
    if not copy_audio:
        cmd += ["-profile:a", "aac_low", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", str(temporary)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode:
            raise RuntimeError("Video conversion failed: " + result.stderr.strip()[-500:])
        output = probe_media(temporary)
        v = next((s for s in output if s["codec_type"] == "video"), None)
        a = next((s for s in output if s["codec_type"] == "audio"), None)
        if (not v or v.get("codec_name") != "h264" or v.get("pix_fmt") != "yuv420p"
                or (audio and not a)
                or (a and (a.get("codec_name") != "aac" or a.get("profile") != "LC"))):
            raise RuntimeError("The converted file did not pass compatibility checks.")
        os.replace(temporary, target)
        if source != target:
            source.unlink()
        return str(target)
    finally:
        temporary.unlink(missing_ok=True)


def parse_ytdlp_json(stdout):
    """Parse yt-dlp JSON output.

    With ``-j`` yt-dlp prints one JSON object per line. Some extractors
    emit multiple videos even with ``--no-playlist``, so stdout contains
    several objects and a plain ``json.loads`` raises "Extra data".
    Return the first valid object.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        return json.loads(line)
    raise ValueError("yt-dlp returned no data")


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    args = ["--no-playlist", "-o", out_template]

    if format_choice == "audio":
        args += ["-x", "--audio-format", "mp3"]
    elif format_id:
        args += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        args += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    try:
        result = run_ytdlp(args, url, DOWNLOAD_TIMEOUT)
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = result.stderr.strip().split("\n")[-1]
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            if not target:
                raise RuntimeError("Download completed but no MP3 file was found")
            chosen = target[0]
        else:
            # Any container yt-dlp leaves behind (.m4v, .3gp, .ts, ...) is a candidate;
            # ffprobe in ensure_compatible_mp4 decides whether it is really video.
            completed = sorted(
                (f for f in files if Path(f).suffix.lower() not in NON_VIDEO_SUFFIXES),
                key=lambda f: Path(f).suffix.lower() != ".mp4",
            )
            if not completed:
                raise RuntimeError("Download completed but no video file was found")
            chosen = ensure_compatible_mp4(completed[0])

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        job["filename"] = safe_filename(job.get("title", ""), ext, os.path.basename(chosen))
        job["status"] = "done"
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download or conversion timed out. Try a lower resolution."
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
    finally:
        # A failed job exposes no file, so nothing could ever retrieve or delete what it left behind.
        if job["status"] != "done":
            for leftover in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")):
                try:
                    os.remove(leftover)
                except OSError:
                    pass


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        result = run_ytdlp(["--no-playlist", "-j"], url, 60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = parse_ytdlp_json(result.stdout)

        # Build quality options — keep best format per resolution
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/playlist", methods=["POST"])
def get_playlist_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["yt-dlp", "--flat-playlist", "-J", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)
        entries = info.get("entries", [])
        urls = [entry.get("url") for entry in entries if entry.get("url")]
        return jsonify({"urls": urls})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching playlist info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    if not os.path.isfile(job["file"]):
        return jsonify({"error": "The download has expired. Download the video again."}), 404
    # Also handle jobs created before the filename fix was applied.
    filename = Path(job["filename"])
    job["filename"] = safe_filename(filename.stem, filename.suffix, os.path.basename(job["file"]))
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
