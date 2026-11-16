"""Site icons for the dashboard (companies, publishers, regulators).

Each domain is looked up once and cached on disk, so the browser only ever talks to our own server.
Lookup order, first real image wins:
  1. Google's favicon service, for the domain and its www. variant
  2. DuckDuckGo's icon service
  3. the site's own /favicon.ico
  4. the icon linked from the site's home page (<link rel="icon" / "apple-touch-icon">)
Both icon services answer 404 (with a placeholder picture) when they have nothing, so a 404 is a miss.
Misses are cached too (for a day) so a site without an icon isn't asked again on every page load.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx

log = logging.getLogger("marketmon.logos")

UA = "SignalStreet-dashboard/1.0 (site icon lookup; personal use)"
HIT_TTL = 30 * 86400
MISS_TTL = 86400
# Public host names only: letters, digits, dots and dashes, ending in a letter TLD. No IPs, no localhost.
DOMAIN_RE = re.compile(r"^(?=.{4,100}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
ICON_LINK = re.compile(r"<link\b[^>]*\brel=[\"']?([^\"'>]*icon[^\"'>]*)[\"']?[^>]*>", re.I)
HREF = re.compile(r"\bhref=[\"']?([^\"'\s>]+)", re.I)
SIZES = re.compile(r"\bsizes=[\"']?(\d+)", re.I)


def sniff(body: bytes) -> str | None:
    """Content type of an image body, or None if it isn't one we serve."""
    if body.startswith(b"\x89PNG"):
        return "image/png"
    if body.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    head = body[:400].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "image/svg+xml"
    return None


def valid_domain(domain: str) -> bool:
    return bool(DOMAIN_RE.match(domain)) and domain != "localhost"


class Logos:
    def __init__(self, cache_dir: Path, client: httpx.Client | None = None):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.Client(timeout=8, follow_redirects=True, headers={"User-Agent": UA})
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # ---------------------------------------------------------------- public
    def get(self, domain: str) -> tuple[bytes, str] | None:
        """(image bytes, content type) for a domain, or None when it has no icon we can find."""
        domain = domain.strip().lower()
        if not valid_domain(domain):
            return None
        with self._guard:
            lock = self._locks.setdefault(domain, threading.Lock())
        with lock:                                   # many <img> tags for one site -> one lookup
            hit, miss = self.dir / f"{domain}.img", self.dir / f"{domain}.miss"
            now = time.time()
            if hit.exists() and now - hit.stat().st_mtime < HIT_TTL:
                body = hit.read_bytes()
                return body, sniff(body) or "image/png"
            if miss.exists() and now - miss.stat().st_mtime < MISS_TTL:
                return None
            found = self._lookup(domain)
            if found:
                hit.write_bytes(found)
                miss.unlink(missing_ok=True)
                return found, sniff(found) or "image/png"
            miss.touch()
            log.info("no icon found for %s", domain)
            return None

    # ---------------------------------------------------------------- lookup
    def _image(self, url: str) -> bytes | None:
        try:
            r = self.client.get(url)
        except httpx.HTTPError:
            return None
        if r.status_code != 200 or len(r.content) < 100:
            return None
        return r.content if sniff(r.content) else None

    def _lookup(self, domain: str) -> bytes | None:
        hosts = [domain] if domain.startswith("www.") else [domain, "www." + domain]
        for h in hosts:
            if body := self._image(f"https://www.google.com/s2/favicons?domain={h}&sz=128"):
                return body
        if body := self._image(f"https://icons.duckduckgo.com/ip3/{domain}.ico"):
            return body
        for h in hosts:
            if body := self._image(f"https://{h}/favicon.ico"):
                return body
        for h in reversed(hosts):                    # home pages usually live on www.
            if body := self._from_home_page(h):
                return body
        return None

    def _from_home_page(self, host: str) -> bytes | None:
        try:
            with self.client.stream("GET", f"https://{host}/") as r:
                if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                    return None
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > 400_000:                # the <head> is near the top
                        break
                html = b"".join(chunks).decode("utf-8", "replace")
                base = str(r.url)
        except httpx.HTTPError:
            return None
        # Prefer apple-touch-icon / the largest declared size: they look best at 24-28 px.
        cands = []
        for m in ICON_LINK.finditer(html):
            tag, rel = m.group(0), m.group(1).lower()
            href = HREF.search(tag)
            if not href or "mask-icon" in rel:
                continue
            size = int(SIZES.search(tag).group(1)) if SIZES.search(tag) else (180 if "apple" in rel else 32)
            cands.append((size, urljoin(base, href.group(1))))
        for _, url in sorted(cands, reverse=True)[:3]:
            if url.startswith("https://") and (body := self._image(url)):
                return body
        return None
