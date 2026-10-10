import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as reclip


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
        self.assertEqual(reclip.jobs["dlfail"]["error"], "ERROR: HTTP Error 403")
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

    def fake_page(self, html):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = html.encode()
        response.headers.get_content_charset.return_value = "utf-8"
        return patch.object(reclip.urllib.request, "urlopen", return_value=response)

    def test_embeds_are_found_metas_first_resolved_and_filtered(self):
        html = """
            <iframe src="//ads.example/frame"></iframe>
            <iframe src="javascript:alert(1)"></iframe>
            <iframe src="/relative/player"></iframe>
            <meta property="og:video" content="https://player.example/embed/9/">
            <meta property="og:video" content="https://player.example/embed/9/">
            <iframe></iframe>
        """
        with self.fake_page(html):
            urls = reclip.find_embed_urls(self.PAGE)
        self.assertEqual(urls, [
            "https://player.example/embed/9/",
            "https://ads.example/frame",
            "https://site.example/relative/player",
        ])

    def test_unreachable_page_yields_no_embeds(self):
        with patch.object(reclip.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertEqual(reclip.find_embed_urls(self.PAGE), [])

    def completed(self, code, stderr=""):
        return subprocess.CompletedProcess([], code, "{}", stderr)

    def test_unsupported_page_retries_embed_with_referer(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return self.completed(1, "ERROR: Unsupported URL: x") if cmd[-1] == self.PAGE else self.completed(0)

        with patch.object(reclip, "find_embed_urls", return_value=["https://p.example/a", "https://p.example/b"]), \
                patch.object(reclip.subprocess, "run", side_effect=fake_run):
            result = reclip.run_ytdlp(["--no-playlist", "-j"], self.PAGE, 60)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls[-1], ["yt-dlp", "--no-playlist", "-j", "--referer", self.PAGE, "https://p.example/a"])
        self.assertEqual(len(calls), 2)

    def test_other_errors_do_not_trigger_embed_search(self):
        with patch.object(reclip, "find_embed_urls", side_effect=AssertionError("must not search")), \
                patch.object(reclip.subprocess, "run", return_value=self.completed(1, "ERROR: HTTP Error 403")):
            self.assertEqual(reclip.run_ytdlp([], self.PAGE, 60).returncode, 1)

    def test_original_error_is_reported_when_no_embed_works(self):
        with patch.object(reclip, "find_embed_urls", return_value=["https://p.example/a"]), \
                patch.object(reclip.subprocess, "run", return_value=self.completed(1, "ERROR: Unsupported URL: x")):
            result = reclip.run_ytdlp([], self.PAGE, 60)
        self.assertIn("Unsupported URL", result.stderr)


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
