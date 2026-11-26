"""Exercise native yt-dlp, the fallback and both download pipelines with owned media."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import app

ROOT = Path(__file__).resolve().parents[1]


class FixtureHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.paths.append(self.path)
        if self.path == "/limited":
            self.send_response(429)
            self.send_header("Retry-After", "17")
            self.end_headers()
            return
        if self.path == "/blocked":
            self.send_response(302)
            self.send_header("Location", "http://localhost/private")
            self.end_headers()
            return
        super().do_GET()


class ExtractionWorkerTests(unittest.TestCase):
    def test_real_downloads_from_embedded_and_direct_media(self):
        with tempfile.TemporaryDirectory() as directory:
            files = Path(directory)
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(ROOT / "assets/preview.mp4"),
                            "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1",
                            "-c:v", "copy", "-c:a", "aac", str(files / "player.mp4")], check=True)
            (files / "watch.html").write_text('<iframe data-src="/player.mp4"></iframe>')
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(FixtureHandler, directory=directory))
            self.addCleanup(server.server_close)
            server.paths = []
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.shutdown)
            port = server.server_port
            # Test-only exception restricted to this fixture's exact socket.
            url = f"http://127.0.0.1:{port}"
            runner = files / "worker.py"
            runner.write_text(f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
import extract_worker as worker
guard = worker.guard_network
def fixture_guard(event, args):
    if event == 'socket.connect' and args[1] == ('127.0.0.1', {port}):
        return
    guard(event, args)
worker.guard_network = fixture_guard
import extraction
original = extraction.valid_url
extraction.valid_url = worker.valid_url = lambda target: target.startswith('{url}/') or original(target)
worker.main()
""")
            run = subprocess.run

            def fixture_run(cmd, **kwargs):
                if len(cmd) > 1 and cmd[1] == str(ROOT / "extract_worker.py"):
                    cmd = [cmd[0], str(runner), *cmd[2:]]
                return run(cmd, **kwargs)

            with patch.object(app.subprocess, "run", side_effect=fixture_run), patch.object(app, "DOWNLOAD_DIR", directory):
                with self.assertRaises(app.RateLimited) as limited:
                    app.run_ytdlp(["-j"], url + "/limited", 60)
                self.assertEqual(limited.exception.retry_after, 17)
                self.assertEqual(server.paths.count("/limited"), 1)
                denied = app.run_ytdlp(["-j"], url + "/blocked", 60)
                self.assertNotEqual(denied.returncode, 0)
                self.assertEqual(app.classify(denied.stderr), "access_denied")
                meta = app.run_ytdlp(["-j"], url + "/watch.html", 60)
                self.assertEqual(meta.returncode, 0, meta.stderr)
                self.assertEqual(server.paths.count("/watch.html"), 1)
                for mode, target, extension in [("video", "/watch.html", ".mp4"), ("audio", "/player.mp4", ".mp3")]:
                    app.jobs[mode] = {"status": "downloading", "title": "Preview"}
                    self.addCleanup(app.jobs.pop, mode, None)
                    app.run_download(mode, url + target, mode, None)
                    job = app.jobs[mode]
                    self.assertEqual(job["status"], "done", job)
                    self.assertEqual(job["filename"], "Preview" + extension)
                    codecs = [s["codec_name"] for s in app.probe_media(job["file"])]
                    self.assertIn("mp3" if mode == "audio" else "h264", codecs)
