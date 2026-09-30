import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit

import httpx


class ToolError(RuntimeError):
    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code)
        self.code, self.retryable = code, retryable


def public_url(url: str, resolver=socket.getaddrinfo) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ToolError("UNSAFE_URL")
    if parsed.port not in {None, 80, 443}:
        raise ToolError("UNSAFE_PORT")
    try:
        addresses = resolver(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
    except OSError:
        raise ToolError("DNS_UNAVAILABLE", True) from None
    if not addresses or any(
        not ipaddress.ip_address(address[4][0]).is_global for address in addresses
    ):
        raise ToolError("PRIVATE_NETWORK_BLOCKED")
    return url


class Transport:
    def __init__(self, client: httpx.Client | None = None, proxy: str = ""):
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            trust_env=False,
            proxy=proxy or None,
        )

    def request(self, method, url, **kwargs):
        for attempt in range(3):
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.HTTPError:
                if attempt == 2:
                    raise ToolError("NETWORK_UNAVAILABLE", True) from None
                time.sleep(0.25 * 2**attempt)
                continue
            if response.status_code in {429, 500, 502, 503, 504}:
                if attempt == 2:
                    raise ToolError(
                        "RATE_LIMIT" if response.status_code == 429 else "UPSTREAM_UNAVAILABLE",
                        True,
                    )
                try:
                    delay = min(30, max(0.25, float(response.headers.get("Retry-After", "1"))))
                except ValueError:
                    delay = 1
                time.sleep(delay)
                continue
            if response.status_code >= 400:
                raise ToolError(f"HTTP_{response.status_code}")
            return response
        raise ToolError("UPSTREAM_UNAVAILABLE", True)

    def public_get(self, url, *, max_bytes=10_000_000, headers_for=None):
        """GET a public URL. `headers_for(url)` supplies per-host headers on every redirect hop."""
        # Production egress proxy additionally enforces destination IPs after DNS resolution.
        for _ in range(6):
            public_url(url)
            try:
                headers = headers_for(url) if headers_for else None
                with self.client.stream("GET", url, headers=headers) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers["location"])
                        continue
                    if response.status_code >= 400:
                        raise ToolError(f"HTTP_{response.status_code}")
                    chunks, length = [], 0
                    for chunk in response.iter_bytes():
                        length += len(chunk)
                        if length > max_bytes:
                            raise ToolError("DOCUMENT_TOO_LARGE")
                        chunks.append(chunk)
                    return (
                        str(response.url),
                        response.headers.get("content-type", ""),
                        b"".join(chunks),
                    )
            except httpx.HTTPError:
                raise ToolError("NETWORK_UNAVAILABLE", True) from None
        raise ToolError("REDIRECT_LIMIT")
