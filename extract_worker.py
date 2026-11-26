"""Run the pinned extractor in a disposable process under a shared deadline."""
import json
import os
import resource
import socket
import sys
from pathlib import Path

import yt_dlp
from yt_dlp.utils import Popen
from yt_dlp.extractor.generic import GenericIE

from extraction import find_embed_urls, public_ip, valid_url


def guard_network(event, args):
    if event == "socket.connect":
        sock, endpoint = args
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            try:
                if public_ip(endpoint[0]):
                    return
            except ValueError:
                pass
        raise PermissionError("Non-public destination blocked")
    if event == "subprocess.Popen":
        executable, argv, *_ = args
        # Native HTTP/HLS/DASH use Python sockets. External tools may only
        # inspect or postprocess local files, never open a network stream.
        if os.path.basename(executable) not in {"ffmpeg", "ffprobe"}:
            raise PermissionError("External downloader disabled")
        if os.path.basename(executable) == "ffprobe" and argv[1:3] != ["-protocol_whitelist", "file,pipe"]:
            raise PermissionError("Unrestricted probe blocked")
        for i, arg in enumerate(argv):
            if os.path.basename(executable) == "ffmpeg" and arg == "-i" and argv[max(0, i-2):i] != ["-protocol_whitelist", "file,pipe"]:
                raise PermissionError("Non-local media input blocked")


def main():
    report = Path(sys.argv[1])
    url = sys.argv[-1]
    original = GenericIE._extract_embeds

    def discover(self, target, webpage, *, urlh=None, info_dict=None):
        entries = list(original(self, target, webpage, urlh=urlh, info_dict=info_dict or {}))
        if not entries and urlh and valid_url(urlh.url):
            candidates = find_embed_urls(urlh.url, webpage)
            if candidates:
                report.write_text(json.dumps([urlh.url, candidates]))
        return entries

    # yt-dlp also invokes ffprobe directly (without postprocessor-args).
    original_popen = Popen.__init__

    def local_media_process(self, args, *pos, **kwargs):
        if os.path.basename(args[0]) == "ffprobe":
            args = [args[0], "-protocol_whitelist", "file,pipe", *args[1:]]
        elif os.path.basename(args[0]) == "ffmpeg":
            restricted = []
            for arg in args:
                if arg == "-i":
                    restricted.extend(["-protocol_whitelist", "file,pipe"])
                restricted.append(arg)
            args = restricted
        original_popen(self, args, *pos, **kwargs)

    original_extractor = yt_dlp.YoutubeDL.get_info_extractor

    def select_extractor(self, key):
        extractor = original_extractor(self, key)
        report.with_suffix(".extractor").write_text(extractor.ie_key())
        return extractor

    Popen.__init__ = local_media_process
    yt_dlp.YoutubeDL.get_info_extractor = select_extractor
    GenericIE._extract_embeds = discover
    sys.addaudithook(guard_network)
    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024**3, 1024**3))
    yt_dlp.main(["--ignore-config", "--no-plugin-dirs", "--no-js-runtimes",
                 "--proxy", "", "--socket-timeout", "15", "--extractor-retries", "0",
                 "--retries", "1", "--fragment-retries", "1", "--max-filesize", "1G",

                 *sys.argv[2:-1], "--", url])


if __name__ == "__main__":
    main()
