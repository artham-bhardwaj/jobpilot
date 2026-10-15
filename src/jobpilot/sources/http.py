"""Polite, defensive HTTP client for job sources.

- Timeouts, bounded retries (honours ``Retry-After``), per-host rate limiting, robots.txt.
- SSRF protection: only ``http(s)``; no credentials in URLs; every request **and every
  redirect hop** must resolve exclusively to globally routable addresses (no loopback,
  private, link-local, shared, reserved or multicast ranges). Redirects are followed
  manually (max ``MAX_REDIRECTS``) so each destination is validated before it is contacted.
- Responses are streamed with a hard byte cap (``MAX_BYTES``; robots.txt ``ROBOTS_MAX_BYTES``);
  an oversized ``Content-Length`` is rejected before any body is read.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
import urllib.robotparser
from urllib.parse import urljoin, urlsplit

import requests

from jobpilot.config import Settings

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_RETRY_AFTER = 60.0
MAX_REDIRECTS = 5
MAX_BYTES = 5_000_000
ROBOTS_MAX_BYTES = 512_000
CHUNK = 64 * 1024


class FetchError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class BlockedByRobots(FetchError):
    pass


class DisallowedAddress(FetchError):
    pass


def _ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _is_public_host(host: str) -> bool:
    """True only if every address the host resolves to is globally routable."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # unresolvable: the request itself will fail with a clear error
    return all(_ip_allowed(ipaddress.ip_address(info[4][0].split("%")[0])) for info in infos)


class PoliteHttp:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": settings.user_agent, "Accept-Language": "en"})
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self.request_count = 0

    # ---- safety checks --------------------------------------------------------------

    def _check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FetchError(f"Unsupported URL: {url}")
        if parts.username or parts.password:
            raise FetchError("URLs containing credentials are not allowed")
        if self.settings.block_private_addresses and not _is_public_host(parts.hostname):
            raise DisallowedAddress(f"Refusing to fetch a local/private network address: {parts.hostname}")

    def _throttle(self, host: str) -> None:
        with self._lock:
            wait = self._last.get(host, 0) + self.settings.http_min_interval_seconds - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    @staticmethod
    def _read_capped(r: requests.Response, max_bytes: int) -> None:
        """Read the streamed body into ``r`` with a hard cap, then release the connection."""
        try:
            declared = r.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise FetchError(f"Response too large ({declared} bytes > {max_bytes}) from {r.url}")
            buf = bytearray()
            for chunk in r.iter_content(CHUNK):
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise FetchError(f"Response exceeded {max_bytes} bytes from {r.url}")
            r._content = bytes(buf)  # noqa: SLF001 - make .text/.json() work after streaming
            r._content_consumed = True  # noqa: SLF001
        finally:
            r.close()

    def _request(self, url: str, *, accept: str, params: dict | None, max_bytes: int,
                 check_robots: bool) -> requests.Response:
        """One logical GET, following redirects manually and validating every hop."""
        for hop in range(MAX_REDIRECTS + 1):
            self._check_url(url)
            if check_robots and not self._robots_allows(url):
                raise BlockedByRobots(f"robots.txt disallows fetching {url}")
            self._throttle(urlsplit(url).netloc)
            r = self.session.get(url, params=params if hop == 0 else None, timeout=self.settings.http_timeout_seconds,
                                 headers={"Accept": accept}, allow_redirects=False, stream=True)
            self.request_count += 1
            if r.status_code in REDIRECT_STATUSES and r.headers.get("Location"):
                nxt = urljoin(r.url or url, r.headers["Location"])
                r.close()
                log.info("http redirect", extra={"from_url": url, "to_url": nxt, "hop": hop + 1})
                url = nxt
                continue
            self._read_capped(r, max_bytes)
            return r
        raise FetchError(f"Too many redirects (>{MAX_REDIRECTS}) starting from {url}")

    def _robots_allows(self, url: str) -> bool:
        if not self.settings.respect_robots_txt:
            return True
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            try:
                r = self._request(f"{base}/robots.txt", accept="text/plain", params=None,
                                  max_bytes=ROBOTS_MAX_BYTES, check_robots=False)
                if r.status_code in (401, 403):
                    rp.disallow_all = True
                elif r.status_code >= 400:
                    rp.allow_all = True
                else:
                    rp.parse(r.text.splitlines())
            except DisallowedAddress:
                raise
            except FetchError as exc:
                # Oversized or otherwise unusable robots.txt: be conservative and don't crawl.
                log.warning("robots.txt unusable; treating site as disallowed", extra={"site": base, "error": str(exc)})
                rp.disallow_all = True
            except requests.RequestException:
                rp = None  # robots unreachable: proceed; the page fetch surfaces real errors
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.settings.user_agent, url)

    def get(self, url: str, *, accept: str = "*/*", params: dict | None = None,
            max_bytes: int = MAX_BYTES) -> requests.Response:
        attempts = self.settings.http_max_retries + 1
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                r = self._request(url, accept=accept, params=params, max_bytes=max_bytes, check_robots=True)
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("http error", extra={"url": url, "attempt": attempt, "error": str(exc)})
                if attempt < attempts:
                    time.sleep(min(2 ** attempt, 10))
                continue
            if r.status_code in RETRY_STATUSES and attempt < attempts:
                try:
                    delay = min(float(r.headers.get("Retry-After", "")), MAX_RETRY_AFTER)
                except ValueError:
                    delay = min(2 ** attempt, 10)
                log.warning("http retry", extra={"url": url, "status": r.status_code, "delay": delay})
                time.sleep(delay)
                continue
            if r.status_code >= 400:
                raise FetchError(f"HTTP {r.status_code} for {url}", status=r.status_code)
            return r
        raise FetchError(f"Failed to fetch {url}: {last_exc}")

    def get_json(self, url: str, params: dict | None = None):
        r = self.get(url, accept="application/json", params=params)
        try:
            return r.json()
        except ValueError as exc:
            raise FetchError(f"Invalid JSON from {url}") from exc
