"""Bounded embed discovery and network policy shared with the extraction worker."""
import ipaddress
import re
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

MAX_PAGE_BYTES = 2_000_000
PLAYER_HINT = re.compile(r"embed|player", re.I)
PLAYER_META = {"og:video", "og:video:url", "og:video:secure_url", "twitter:player"}


def public_ip(host):
    address = ipaddress.ip_address(host)
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            address = address.ipv4_mapped
        elif (address.sixtofour or address.teredo or
              address in ipaddress.ip_network("64:ff9b::/96")):
            return False
    return address.is_global and not address.is_multicast


def valid_url(url):
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if (not host or parts.scheme not in {"http", "https"} or parts.username is not None
                or "__youtubedl_smuggle" in parts.fragment or parts.port == 0 or "\\" in url or "%" in host
                or any(c.isspace() or ord(c) < 32 for c in url)):
            return False
        try:
            return public_ip(host)
        except ValueError:
            host = host.rstrip(".").lower()
            return "." in host and not host.endswith((".localhost", ".local", ".internal"))
    except ValueError:
        return False


class EmbedFinder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.players = []
        self.frames = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "iframe":
            self.frames.append(attrs.get("src") or attrs.get("data-src"))
        elif tag == "meta" and (attrs.get("property") or attrs.get("name")) in PLAYER_META:
            self.players.append(attrs.get("content"))


def find_embed_urls(page_url, html):
    if len(html.encode("utf-8")) > MAX_PAGE_BYTES:
        return []
    finder = EmbedFinder()
    finder.feed(html)
    found = []
    for candidates in (finder.players, finder.frames):
        for raw in candidates:
            if not raw:
                continue
            try:
                url = urldefrag(urljoin(page_url, raw.strip())).url
                parts = urlsplit(url)
            except ValueError:
                continue
            if (valid_url(url) and url != page_url and url not in found
                    and (candidates is finder.players or PLAYER_HINT.search(parts.netloc + parts.path))):
                found.append(url)
                if len(found) == 3:
                    return found
    return found
