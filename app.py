import os
import re
import uuid
import glob
import json
import subprocess
import threading
import time
import sys
import tempfile
from importlib.metadata import version
from math import ceil
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from flask import Flask, request, jsonify, send_file, render_template
from werkzeug.http import parse_date

from extraction import INSTAGRAM_HOSTS, valid_url

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
YTDLP_VERSION = version("yt-dlp")
INSTAGRAM_RETRY_DELAY = 60
instagram_retry_at = 0
instagram_retry_lock = threading.Lock()
ERRORS = {
    "invalid_url": (400, "Enter a valid public HTTP or HTTPS URL."),
    "unsupported": (422, "This URL is not supported."),
    "access_denied": (403, "Access denied by the platform or network policy."),
    "login_required": (403, "Instagram asked ReClip to sign in. Open the Reel in a private browser window to check if it is publicly accessible."),
    "unavailable": (404, "This content is no longer available."),
    "extraction": (502, "Could not extract media from this page."),
    "timeout": (504, "The request timed out. Try again later."),
    "rate_limit": (429, "The platform is receiving too many requests. Try again later."),
    "internal": (500, "ReClip could not finish processing this request."),
}


class RateLimited(Exception):
    def __init__(self, extractor, retry_after):
        message = ERRORS["rate_limit"][1]
        if retry_after:
            seconds = "second" if retry_after == 1 else "seconds"
            message = f"The platform is receiving too many requests. Try again in {retry_after} {seconds}."
        elif retry_after == 0:
            message = "The platform is receiving too many requests. You can try again now."
        super().__init__(message)
        self.extractor = extractor
        self.retry_after = retry_after


def classify(stderr):
    # Ignore diagnostics preceding the final error and URL text in the error.
    message = re.sub(r"https?://\S+", "", stderr.lower().rsplit("error: ", 1)[-1])
    if "http error 429" in message:
        return "rate_limit"
    if "[instagram]" in message and "redirected to the login page" in message:
        return "login_required"
    for category, patterns in (
        ("rate_limit", ("too many requests", "rate limit", "rate-limit")),
        ("access_denied", ("http error 403", "http error 401", "private video", "sign in",
                           "login required", "drm", "non-public destination", "geo-restricted")),
        ("timeout", ("timed out", "timeout")),
        ("unavailable", ("http error 404", "http error 410", "video unavailable", "has been removed")),
        ("unsupported", ("unsupported url",)),
    ):
        if any(token in message for token in patterns):
            return category
    return "extraction"


def record(url, operation, started, category=None, extractor=None):
    print(json.dumps({"operation": operation, "domain": urlsplit(url).hostname if valid_url(url) else None,
                      "extractor": extractor, "error_category": category,
                      "duration_ms": round((time.monotonic() - started) * 1000),
                      "yt_dlp_version": YTDLP_VERSION}), flush=True)


def api_error(category, rate_limit=None):
    status, message = ERRORS[category]
    if rate_limit is None:
        return jsonify(error=message, category=category), status
    response = jsonify(error=str(rate_limit), category=category, retry_after=rate_limit.retry_after)
    if rate_limit.retry_after is not None:
        response.headers["Retry-After"] = str(rate_limit.retry_after)
    return response, status


def parse_retry_after(value):
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    date = parse_date(value)
    return max(0, ceil(date.timestamp() - time.time())) if date else None


def run_ytdlp(args, url, timeout):
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory() as directory:
        report = Path(directory) / "embeds.json"
        trace = report.with_suffix(".extractor")
        rate_limit_report = report.with_suffix(".http-429.json")

        def run(extra, target):
            global instagram_retry_at
            instagram = urlsplit(target).hostname in INSTAGRAM_HOSTS
            if instagram:
                with instagram_retry_lock:
                    remaining = ceil(instagram_retry_at - time.monotonic())
                if remaining > 0:
                    raise RateLimited("Instagram", remaining)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("yt-dlp", timeout)
            trace.unlink(missing_ok=True)
            rate_limit_report.unlink(missing_ok=True)
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("extract_worker.py")),
                                     str(report), *args, *extra, target],
                                    capture_output=True, text=True, timeout=remaining)
            result.extractor = trace.read_text() if trace.exists() else None
            if result.returncode and classify(result.stderr) == "rate_limit":
                host, header = None, ""
                if rate_limit_report.exists():
                    host, header = json.loads(rate_limit_report.read_text())
                retry_after = parse_retry_after(header)
                if instagram and host in INSTAGRAM_HOSTS:
                    if retry_after is None:
                        retry_after = INSTAGRAM_RETRY_DELAY
                    with instagram_retry_lock:
                        now = time.monotonic()
                        instagram_retry_at = max(instagram_retry_at, now + retry_after)
                        retry_after = ceil(instagram_retry_at - now)
                raise RateLimited(result.extractor, retry_after)
            return result

        result = run([], url)
        if result.returncode and classify(result.stderr) == "unsupported" and report.exists():
            page, candidates = json.loads(report.read_text())
            parts = urlsplit(page)
            for candidate in candidates:
                referer = urlunsplit((parts.scheme, parts.netloc,
                                      parts.path if urlsplit(candidate)[:2] == parts[:2] else "/",
                                      "", ""))
                result = run(["--referer", referer], candidate)
                if not result.returncode or classify(result.stderr) != "unsupported":
                    break
        return result


def safe_filename(title, extension, fallback):
    """Build a title-based filename that is safe for a download header."""
    title = " ".join((title or "").split())
    title = "".join(c for c in title if c not in r'\/:*?"<>|' and ord(c) >= 32 and ord(c) != 127)
    title = title[:100].strip(" .")
    return f"{title}{extension}" if title else fallback


def probe_media(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Could not inspect the downloaded video.")
    return json.loads(result.stdout)["streams"]


def ensure_compatible_mp4(filename):
    """Produce H.264/AAC MP4; copy streams that already meet the requirements."""
    source = Path(filename)
    streams = probe_media(source)
    # Cover art is stored as a video stream flagged attached_pic; it is not the video.
    video = next((s for s in streams if s["codec_type"] == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    if not video:
        raise RuntimeError("The downloaded file has no video track.")
    copy_video = (
        video.get("codec_name") == "h264" and video.get("pix_fmt") == "yuv420p"
        and video.get("profile") in {"Baseline", "Constrained Baseline", "Main", "High"}
    )
    copy_audio = not audio or (audio.get("codec_name") == "aac" and audio.get("profile") == "LC")
    target = source.with_suffix(".mp4")
    temporary = source.with_name(source.stem + ".compatible.mp4")
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-protocol_whitelist", "file,pipe", "-i", str(source),
           "-map", f"0:{video['index']}", "-map", "0:a:0?", "-c:v", "copy" if copy_video else "libx264"]
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
    started = time.monotonic()
    extractor = None
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
        extractor = result.extractor
        if result.returncode != 0:
            category = classify(result.stderr)
            job.update(category=category, error=ERRORS[category][1], status="error")
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            raise RuntimeError("Missing downloaded file")

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
    except RateLimited as error:
        extractor = error.extractor
        job.update(category="rate_limit", error=str(error), retry_after=error.retry_after, status="error")
    except subprocess.TimeoutExpired:
        job.update(category="timeout", error=ERRORS["timeout"][1], status="error")
    except Exception:
        job.update(category="internal", error=ERRORS["internal"][1], status="error")
    finally:
        record(url, "download", started, job.get("category"), extractor)
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
    return extract_metadata("info")


def request_data():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def request_url(data):
    value = data.get("url")
    return value.strip() if isinstance(value, str) else ""


def extract_metadata(operation):
    started = time.monotonic()
    url = request_url(request_data())
    category = extractor = rate_limit = None
    try:
        if not valid_url(url):
            category = "invalid_url"
            return api_error(category)
        args = ["--flat-playlist", "-J"] if operation == "playlist" else ["--no-playlist", "-j"]
        result = run_ytdlp(args, url, 60)
        extractor = result.extractor
        if result.returncode:
            category = classify(result.stderr)
            return api_error(category)
        info = parse_ytdlp_json(result.stdout)
        extractor = extractor or info.get("extractor_key")
        if operation == "playlist":
            return jsonify(urls=[e["url"] for e in info.get("entries", []) if e and e.get("url")])

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
    except RateLimited as error:
        category, extractor, rate_limit = "rate_limit", error.extractor, error
    except subprocess.TimeoutExpired:
        category = "timeout"
    except (ValueError, KeyError):
        category = "extraction"
    except Exception:
        category = "internal"
    finally:
        record(url, operation, started, category, extractor)
    return api_error(category, rate_limit)


@app.route("/api/playlist", methods=["POST"])
def get_playlist_info():
    return extract_metadata("playlist")


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request_data()
    url = request_url(data)
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not valid_url(url):
        return api_error("invalid_url")

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "title": title}

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
        "category": job.get("category"),
        "retry_after": job.get("retry_after"),
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
