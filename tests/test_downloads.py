import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as reclip
from extraction import find_embed_urls, valid_url


class DownloadResponseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.file = Path(self.directory.name) / "video.mp4"
        self.file.write_bytes(b"test media")
        self.client = reclip.app.test_client()

    def tearDown(self):
        reclip.jobs.clear()
        self.directory.cleanup()

    def test_linkedin_title_with_line_breaks_can_be_downloaded(self):
        reclip.jobs["linkedin"] = {
            "status": "done", "file": str(self.file),
            "filename": "What are Salesforce Microfrontends?\n\nMicrofrontends are new.mp4",
        }
        for method in (self.client.head, self.client.get):
            with method("/api/file/linkedin") as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "video/mp4")
                disposition = response.headers["Content-Disposition"]
                self.assertNotIn("\n", disposition)
                self.assertNotIn("\r", disposition)
                self.assertIn("Salesforce Microfrontends Microfrontends are new.mp4", disposition)

    def test_expired_download_returns_error_instead_of_html_500(self):
        reclip.jobs["expired"] = {
            "status": "done", "file": str(self.file) + ".missing", "filename": "video.mp4",
        }
        response = self.client.get("/api/file/expired")
        self.assertEqual(response.status_code, 404)
        self.assertIn("expired", response.json["error"])

    def test_missing_and_in_progress_jobs_cannot_be_saved(self):
        self.assertEqual(self.client.get("/api/file/missing").status_code, 404)
        reclip.jobs["progress"] = {"status": "downloading"}
        self.assertEqual(self.client.head("/api/file/progress").status_code, 404)

    def test_filename_keeps_unicode_and_extension_without_control_characters(self):
        value = reclip.safe_filename('  José\r\n\tSalesforce\x00\x7f /:*?"<>|  ', ".mp4", "fallback.mp4")
        self.assertEqual(value, "José Salesforce.mp4")
        self.assertEqual(reclip.safe_filename("\n\x00...", ".mp4", "fallback.mp4"), "fallback.mp4")
        self.assertEqual(len(reclip.safe_filename("x" * 150, ".mp4", "fallback.mp4")), 104)

    def test_worker_only_publishes_readiness_after_conversion(self):
        job_id = "worker"
        reclip.jobs[job_id] = {"status": "downloading", "title": "A\n\nB"}
        downloaded = Path(self.directory.name) / f"{job_id}.mp4"
        downloaded.write_bytes(b"media")

        def conversion(path):
            self.assertEqual(reclip.jobs[job_id]["status"], "downloading")
            self.assertNotIn("filename", reclip.jobs[job_id])
            return path

        with patch.object(reclip, "DOWNLOAD_DIR", self.directory.name), \
                patch.object(reclip.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(reclip, "ensure_compatible_mp4", side_effect=conversion):
            reclip.run_download(job_id, "https://example.org/video", "video", "1080")
        self.assertEqual(reclip.jobs[job_id]["status"], "done")
        self.assertEqual(reclip.jobs[job_id]["filename"], "A B.mp4")

    def test_conversion_failure_never_reports_done(self):
        reclip.jobs["failed"] = {"status": "downloading", "title": "Video"}
        (Path(self.directory.name) / "failed.mp4").write_bytes(b"media")
        with patch.object(reclip, "DOWNLOAD_DIR", self.directory.name), \
                patch.object(reclip.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(reclip, "ensure_compatible_mp4", side_effect=RuntimeError("Conversion failed")):
            reclip.run_download("failed", "https://example.org/video", "video", None)
        self.assertEqual(reclip.jobs["failed"]["status"], "error")
        self.assertNotIn("file", reclip.jobs["failed"])

    def test_failed_jobs_leave_no_files_behind(self):
        failures = {
            "convert": dict(ensure=RuntimeError("Conversion failed")),
            "timeout": dict(ensure=subprocess.TimeoutExpired("ffmpeg", 600)),
        }
        for name, failure in failures.items():
            with self.subTest(name):
                job_id = "leak" + name
                reclip.jobs[job_id] = {"status": "downloading", "title": "Video"}
                files = [Path(self.directory.name) / f"{job_id}.{suffix}" for suffix in ("webm", "mp4.part", "jpg")]
                for file in files:
                    file.write_bytes(b"media")
                with patch.object(reclip, "DOWNLOAD_DIR", self.directory.name), \
                        patch.object(reclip.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                        patch.object(reclip, "ensure_compatible_mp4", side_effect=failure["ensure"]):
                    reclip.run_download(job_id, "https://example.org/video", "video", None)
                self.assertEqual(reclip.jobs[job_id]["status"], "error")
                self.assertEqual([f for f in files if f.exists()], [])

    def test_failed_download_removes_partial_files(self):
        reclip.jobs["dlfail"] = {"status": "downloading", "title": "Video"}
        partial = Path(self.directory.name) / "dlfail.mp4.part"
        partial.write_bytes(b"incomplete")
        with patch.object(reclip, "DOWNLOAD_DIR", self.directory.name), \
                patch.object(reclip.subprocess, "run",
                             return_value=subprocess.CompletedProcess([], 1, "", "ERROR: HTTP Error 403")):
            reclip.run_download("dlfail", "https://example.org/video", "video", None)
        self.assertEqual(reclip.jobs["dlfail"]["category"], "access_denied")
        self.assertFalse(partial.exists())

    def test_successful_job_keeps_its_file(self):
        reclip.jobs["keep"] = {"status": "downloading", "title": "Video"}
        kept = Path(self.directory.name) / "keep.mp4"
        kept.write_bytes(b"media")
        with patch.object(reclip, "DOWNLOAD_DIR", self.directory.name), \
                patch.object(reclip.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(reclip, "ensure_compatible_mp4", return_value=str(kept)):
            reclip.run_download("keep", "https://example.org/video", "video", None)
        self.assertEqual(reclip.jobs["keep"]["status"], "done")
        self.assertTrue(kept.exists())

    def test_cover_art_listed_first_is_not_mapped_as_the_video(self):
        source = Path(self.directory.name) / "cover_first.mp4"
        source.write_bytes(b"media")
        cover = {"index": 0, "codec_type": "video", "codec_name": "png", "disposition": {"attached_pic": 1}}
        video = {"index": 1, "codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
                 "profile": "High", "disposition": {"attached_pic": 0}}
        audio = {"index": 2, "codec_type": "audio", "codec_name": "aac", "profile": "LC"}
        commands = []

        def fake_ffmpeg(cmd, **kwargs):
            commands.append(cmd)
            Path(cmd[-1]).write_bytes(b"converted")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(reclip, "probe_media", side_effect=[[cover, video, audio], [video, audio]]),                 patch.object(reclip.subprocess, "run", side_effect=fake_ffmpeg):
            reclip.ensure_compatible_mp4(source)
        command = commands[0]
        self.assertEqual(command[command.index("-map") + 1], "0:1", "must map the real video, not the cover")
        self.assertNotIn("0:0", command)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")

    def test_partial_files_are_never_published(self):
        reclip.jobs["partial"] = {"status": "downloading", "title": "Video"}
        (Path(self.directory.name) / "partial.mp4.part").write_bytes(b"incomplete")
        with patch.object(reclip, "DOWNLOAD_DIR", self.directory.name), \
                patch.object(reclip.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            reclip.run_download("partial", "https://example.org/video", "video", None)
        self.assertEqual(reclip.jobs["partial"]["status"], "error")

    def test_less_common_containers_are_sent_to_conversion_not_rejected(self):
        for suffix in (".m4v", ".3gp", ".ts"):
            with self.subTest(suffix=suffix):
                job_id = "container" + suffix.strip(".")
                reclip.jobs[job_id] = {"status": "downloading", "title": "Clip"}
                source = Path(self.directory.name) / f"{job_id}{suffix}"
                source.write_bytes(b"media")
                (Path(self.directory.name) / f"{job_id}.jpg").write_bytes(b"thumbnail")
                converted = Path(self.directory.name) / f"{job_id}.mp4"
                with patch.object(reclip, "DOWNLOAD_DIR", self.directory.name), \
                        patch.object(reclip.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                        patch.object(reclip, "ensure_compatible_mp4", return_value=str(converted)) as convert:
                    reclip.run_download(job_id, "https://example.org/video", "video", None)
                convert.assert_called_once_with(str(source))
                self.assertEqual(reclip.jobs[job_id]["status"], "done")
                self.assertEqual(reclip.jobs[job_id]["filename"], "Clip.mp4")

    def test_mp3_download_skips_video_conversion(self):
        reclip.jobs["audio"] = {"status": "downloading", "title": "Audio"}
        (Path(self.directory.name) / "audio.mp3").write_bytes(b"audio")
        with patch.object(reclip, "DOWNLOAD_DIR", self.directory.name), \
                patch.object(reclip.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(reclip, "ensure_compatible_mp4", side_effect=AssertionError("MP3 must not be converted")):
            reclip.run_download("audio", "https://example.org/video", "audio", None)
        self.assertEqual(reclip.jobs["audio"]["status"], "done")
        self.assertEqual(reclip.jobs["audio"]["filename"], "Audio.mp3")


class EmbeddedPlayerFallbackTests(unittest.TestCase):
    PAGE = "https://site.example/video/1/clip/"

    def test_relative_and_lazy_players_resolve_against_final_page(self):
        self.assertEqual(find_embed_urls(self.PAGE, """
            <iframe src="/ads"></iframe><iframe data-src="/player/77"></iframe>
            <meta property="og:video" content="/embed/9">
            <iframe src="/embed/9"></iframe><iframe src="http://127.0.0.1/player"></iframe>
        """), ["https://site.example/embed/9", "https://site.example/player/77"])

    def test_denied_embed_preserves_error_and_stops(self):
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if len(calls) == 1:
                Path(cmd[2]).write_text(json.dumps([self.PAGE + "?token=private",
                    ["https://external.example/player", "https://site.example/embed/other"]]))
                return subprocess.CompletedProcess(cmd, 1, "", "ERROR: Unsupported URL")
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: HTTP Error 403")

        with patch.object(reclip.subprocess, "run", side_effect=run):
            result = reclip.run_ytdlp([], self.PAGE, 60)
        self.assertEqual(len(calls), 2)
        self.assertIn("https://site.example/", calls[1])
        self.assertEqual(reclip.classify(result.stderr), "access_denied")

    def test_errors_and_redaction(self):
        for category, message in [
            ("rate_limit", "HTTP Error 429"), ("unavailable", "HTTP Error 410"),
            ("access_denied", "HTTP Error 401"), ("timeout", "timed out"),
            ("unsupported", "Unsupported URL"), ("extraction", "unknown failure"),
        ]:
            error = subprocess.CompletedProcess([], 1, "", message + " https://site.example/?secret")
            with patch.object(reclip.subprocess, "run", return_value=error) as run, patch("builtins.print") as log:
                response = reclip.app.test_client().post("/api/info", json={"url": self.PAGE+"?secret"})
            self.assertEqual(response.json["category"], category)
            self.assertNotIn("secret", response.text + str(log.call_args))
            run.assert_called_once()

    def test_worker_timeout_and_internal_failure_have_distinct_responses(self):
        for exception, category, status in [
            (subprocess.TimeoutExpired("worker", 60), "timeout", 504),
            (FileNotFoundError("worker"), "internal", 500),
        ]:
            with patch.object(reclip.subprocess, "run", side_effect=exception):
                response = reclip.app.test_client().post("/api/info", json={"url": self.PAGE})
            self.assertEqual((response.json["category"], response.status_code), (category, status))

    def test_named_platforms_still_dispatch_to_native_extractors(self):
        from yt_dlp.extractor import gen_extractor_classes
        extractors = gen_extractor_classes()
        for key, url in [
            ("Youtube", "https://www.youtube.com/watch?v=BaW_jenozKc"),
            ("Vimeo", "https://vimeo.com/1084537"),
            ("Instagram", "https://www.instagram.com/p/CHfixture01/"),
            ("TikTok", "https://www.tiktok.com/@fixture/video/1234567890123456789"),
        ]:
            self.assertTrue(valid_url(url))
            ie = next(ie for ie in extractors if ie.suitable(url))
            self.assertEqual(ie.ie_key(), key)

    def test_invalid_input_and_network_policy(self):
        for url in ["file:///etc", "http://localhost", "http://127.0.0.1/a",
                    "http://[::ffff:10.0.0.1]", "https://user:pass@site.example"]:
            self.assertFalse(valid_url(url))
            for route in ("info", "playlist", "download"):
                response = reclip.app.test_client().post("/api/" + route, json={"url": url})
                self.assertEqual(response.json["category"], "invalid_url")
        from extract_worker import guard_network
        import socket
        with socket.socket() as sock:
            for host in ("127.0.0.1", "169.254.169.254", "10.0.0.1", "::1"):
                with self.assertRaises(PermissionError):
                    guard_network("socket.connect", (sock, (host, 443)))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe required")
class MediaCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def make_video(self, filename, video_codec, audio_codec=None, pixel_format="yuv420p", size="160x120"):
        path = Path(self.directory.name) / filename
        cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
               f"testsrc2=size={size}:rate=10"]
        if audio_codec:
            cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
        cmd += ["-t", "0.4", "-c:v", video_codec, "-pix_fmt", pixel_format, "-threads", "1"]
        if audio_codec:
            cmd += ["-c:a", audio_codec]
        cmd += [str(path)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        return path

    def assert_compatible(self, path, expect_audio=True):
        streams = reclip.probe_media(path)
        video = next(s for s in streams if s["codec_type"] == "video")
        audio = next((s for s in streams if s["codec_type"] == "audio"), None)
        self.assertEqual(video["codec_name"], "h264")
        self.assertEqual(video["pix_fmt"], "yuv420p")
        if expect_audio:
            self.assertIsNotNone(audio)
            self.assertEqual(audio["codec_name"], "aac")
            self.assertEqual(audio["profile"], "LC")
        data = Path(path).read_bytes()
        self.assertLess(data.index(b"moov"), data.index(b"mdat"), "MP4 must support fast start")
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
                       check=True, capture_output=True, timeout=30)

    def test_vp9_opus_is_converted_to_h264_aac_mp4(self):
        source = self.make_video("input.webm", "libvpx-vp9", "libopus")
        output = reclip.ensure_compatible_mp4(source)
        self.assertEqual(Path(output).suffix, ".mp4")
        self.assertFalse(source.exists())
        self.assert_compatible(output)
        self.assertFalse(list(Path(self.directory.name).glob("*.compatible.mp4")))

    def test_compatible_video_is_remuxed_without_reencoding(self):
        source = self.make_video("compatible.mp4", "libx264", "aac")
        packet_cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_packets",
                      "-show_data_hash", "sha256", "-show_entries", "packet=data_hash", "-of", "json", str(source)]
        before = json.loads(subprocess.run(packet_cmd, check=True, capture_output=True, text=True).stdout)
        output = reclip.ensure_compatible_mp4(source)
        after = json.loads(subprocess.run(packet_cmd, check=True, capture_output=True, text=True).stdout)
        self.assertEqual(before, after)
        self.assert_compatible(output)

    def test_video_without_audio_remains_valid(self):
        source = self.make_video("silent.mkv", "libx264", pixel_format="yuv444p")
        self.assert_compatible(reclip.ensure_compatible_mp4(source), expect_audio=False)

    def test_audio_only_mp4_is_rejected(self):
        source = Path(self.directory.name) / "audio.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440",
                        "-t", "0.4", "-c:a", "aac", str(source)], check=True, capture_output=True, timeout=30)
        with self.assertRaisesRegex(RuntimeError, "no video track"):
            reclip.ensure_compatible_mp4(source)


if __name__ == "__main__":
    unittest.main()
