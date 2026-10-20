"""web_search + fetch_url handlers — real, keyless web search (stdlib only).

Three no-key backends (research doc: web search must work without an external
MCP remote):

- ``kind="web"``       -> DuckDuckGo HTML search (POST /html/), parse top results
- ``kind="news"``      -> Google News RSS (news.google.com/rss/search), latest headlines
- ``kind="wikipedia"`` -> Wikipedia Search API (JSON), encyclopedia entries

``fetch_url`` downloads a page and returns a trimmed plain-text extract for
reading content that a search snippet can't convey.

SSRF guard (2026-10-20): fetched content is untrusted; a plain HTTP uploader
lets a model (or search-result content) probe the local/private network.
Every transport hop therefore goes through ``_make_guard``:

- scheme must be http(s) — enforced on the initial URL AND every redirect
- the destination host is resolved and checked AFTER DNS resolution: private
  (RFC1918 / CGNAT / unique-local IPv6), loopback, link-local (incl. the cloud
  metadata services 169.254.169.254 and fd00:ec2::254), reserved, multicast,
  and unspecified addresses are rejected — for IP-literal hosts directly,
  otherwise against the full resolver answer list
- redirects are followed MANUALLY (the automattic redirect handler is
  disabled) so each Location target re-runs the same guard; 301/302/303 drop
  the method/body to GET, 307/308 preserve them
- every response body is capped at ``RESPONSE_BYTE_CAP`` bytes and the
  returned text stays capped at the tool's ``max_chars``

Residual risk (documented, not denied): the DNS answer is checked but the
connection still opens by hostname, so a rebinding resolver could serve one
address to the guard and another to the connect call. Closing that needs an
IP-pinned connection (custom HTTP(S)Connection); the guard + cap materially
reduces the attack surface in the meantime and the same seam pattern applies
when pinning gets implemented.

All network calls go through injectable ``urlopen`` and ``resolver`` seams so
tests supply fakes without touching the network or live DNS (mirrors
run_code's injectable docker client). The module stays stdlib-only so it
ports to the browser worker (which proxies through web/server.py's /api/search
to avoid CORS).

Schema (registry.json): ``web_search(query, kind?, max_results?)`` and
``fetch_url(url, max_chars?)``. Neither requires approval (read-only).
"""

from __future__ import annotations

import html
import http.client
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable

DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

KINDS = {"web", "news", "wikipedia"}

# Guard knobs. Lower the byte cap only for debugging; the tool never needs a
# page larger than this to produce a useful extract.
MAX_REDIRECTS = 5
RESPONSE_BYTE_CAP = 1_000_000
REDIRECT_CODES = {301, 302, 303, 307, 308}

# Metadata-service literal addresses beyond the ipaddress module classes
# (is_link_local already covers IPv4 169.254/16; fd00::/8 unique-local is
# caught by is_private). Listed explicitly so the rejection reason says
# "metadata" instead of "private" in error text: AWS's and Aliyun's ECS
# endpoints.
METADATA_LITERALS = frozenset(
    {
        "169.254.169.254",
        "fd00:ec2::254",
        "100.100.100.200",
    }
)

_MISSING = object()


class FetchBlocked(ValueError):
    """raised by the SSRF guard when a destination is rejected."""


# ---------- SSRF destination guard ----------

def _ip_reason(ip: ipaddress._BaseAddress) -> str | None:
    """Return why this address may not be fetched, or None when external."""
    if str(ip) in METADATA_LITERALS:
        return "metadata service"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_private:
        return "private address"
    if ip.is_reserved:
        return "reserved address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    return None


def _make_guard(resolver: Callable | None = None) -> Callable[[str], None]:
    """Build ``guard(url) -> None`` (raises FetchBlocked on a bad destination).

    ``resolver`` mirrors ``socket.getaddrinfo(host, port)``; inject a fake in
    tests. No network is touched when the URL host is an IP literal or a
    scheme/host problem already blocks it.
    """
    resolve = resolver or socket.getaddrinfo

    def guard(url: str) -> None:
        parsed = urllib.parse.urlsplit(str(url))
        if parsed.scheme not in ("http", "https"):
            raise FetchBlocked(
                f"blocked destination: scheme {parsed.scheme!r} is not http/https ({url!r})"
            )
        host = (parsed.hostname or "").strip(".").lower()
        if not host:
            raise FetchBlocked(f"blocked destination: {url!r} has no host")

        literal = None
        try:
            literal = ipaddress.ip_address(host)  # v4 or v6 IP literal
        except ValueError:
            pass
        if literal is not None:
            addresses = [literal]
        else:
            try:
                infos = resolve(host, 0)
            except Exception as exc:  # noqa: BLE001 - DNS failure = no destination
                raise FetchBlocked(
                    f"blocked destination: DNS resolution failed for {host!r} ({type(exc).__name__}: {exc})"
                )
            if not infos:
                raise FetchBlocked(f"blocked destination: {host!r} resolved to no addresses")
            addresses = [ipaddress.ip_address(info[4][0]) for info in infos]

        for ip in addresses:
            reason = _ip_reason(ip)
            if reason is not None:
                raise FetchBlocked(
                    f"blocked destination: {ip} of {host!r} is a {reason} "
                    "(local/private networks and metadata services are not fetchable)"
                )

    return guard


# ---------- low-level transport (guard + manual redirects + byte cap) ----------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable urllib's auto-follow so every redirect surfaces as HTTPError
    and the loop below can run the guard on each Location target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def _default_open(request, timeout: float):
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def _request(url: str, body: bytes | None, method: str) -> urllib.request.Request:
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    return urllib.request.Request(url, data=body, headers=headers, method=method)


def _read_capped(resp: Any, cap: int = RESPONSE_BYTE_CAP) -> str:
    """Read at most ``cap`` bytes, then decode. Over the cap => FetchBlocked."""
    buffer = bytearray()
    while True:
        try:
            chunk = resp.read(65536)
        except http.client.IncompleteRead as exc:
            # Connection-close-delimited body: http.client complains when
            # read(n) runs past EOF instead of returning b"". The partial
            # payload IS the EOF data -- take it and stop looping.
            buffer.extend(getattr(exc, "partial", b"") or b"")
            break
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > cap:
            raise FetchBlocked(f"response exceeds the {cap}-byte download cap")
    return bytes(buffer).decode("utf-8", "replace")


def _open(
    url: str,
    urlopen: Callable | None = None,
    resolver: Callable | None = None,
    data: bytes | None = None,
) -> str:
    """Fetch ``url`` through the guard, following redirects manually."""
    guard = _make_guard(resolver)
    current = url
    method = "POST" if data else "GET"
    body = data
    hops = 0
    while True:
        guard(current)  # initial URL + every redirect target
        if hops >= MAX_REDIRECTS:
            raise FetchBlocked(f"more than {MAX_REDIRECTS} redirects; refusing to follow")
        open_fn = urlopen or _default_open
        try:
            resp = open_fn(_request(current, body, method), timeout=15)
        except urllib.error.HTTPError as exc:
            location = (exc.headers or {}).get("Location")
            if exc.code not in REDIRECT_CODES or not location:
                raise
            exc.close()
            if exc.code in (301, 302, 303):  # redirect semantics: start over with GET
                method, body = "GET", None
            current = urllib.parse.urljoin(current, location.strip())
            hops += 1
            continue
        # Injected fakes may signal redirects via attributes instead of HTTPError.
        location = None
        headers = getattr(resp, "headers", None)
        status = getattr(resp, "status", _MISSING)
        if status is _MISSING and headers is not None and getattr(headers, "getstatus", None):
            status = int(headers.getstatus() or 0)
        if status in REDIRECT_CODES and headers is not None:
            location = headers.get("Location")
            if not location:
                raise urllib.error.HTTPError(current, status, "redirect without Location", headers, None)
        if location is not None:
            if status in (301, 302, 303):
                method, body = "GET", None
            current = urllib.parse.urljoin(current, location.strip())
            hops += 1
            continue
        try:
            with resp:
                return _read_capped(resp)
        except urllib.error.HTTPError:
            raise


def make_handler(
    urlopen: Callable | None = None,
    resolver: Callable | None = None,
) -> Callable[[dict[str, Any]], str]:
    """Return a ToolRegistry handler that performs live web search.

    ``urlopen`` mirrors ``urllib.request.urlopen`` (request, timeout) -> file-like.
    When omitted, the guarded stdlib opener is used.
    """

    def handler(arguments: dict[str, Any]) -> str:
        kind = arguments.get("kind", "web")
        if kind not in KINDS:
            return f"error: unknown kind '{kind}' (expected one of {sorted(KINDS)})"
        query = arguments.get("query", "")
        if not query.strip():
            return "error: empty query"
        max_results = int(arguments.get("max_results", 5))
        if max_results < 1 or max_results > 10:
            return "error: max_results must be between 1 and 10"
        try:
            if kind == "web":
                return _search_web(query, max_results, urlopen, resolver)
            if kind == "news":
                return _search_news(query, max_results, urlopen, resolver)
            return _search_wikipedia(query, max_results, urlopen, resolver)
        except Exception as exc:  # noqa: BLE001
            return f"error: web search failed: {type(exc).__name__}: {exc}"

    return handler


def make_fetch_handler(
    urlopen: Callable | None = None,
    resolver: Callable | None = None,
) -> Callable[[dict[str, Any]], str]:
    def handler(arguments: dict[str, Any]) -> str:
        url = arguments.get("url", "")
        if not url.startswith(("http://", "https://")):
            return "error: url must start with http:// or https://"
        max_chars = int(arguments.get("max_chars", 4000))
        if max_chars < 200 or max_chars > 20000:
            return "error: max_chars must be between 200 and 20000"
        try:
            return _fetch_page(url, max_chars, urlopen, resolver)
        except Exception as exc:  # noqa: BLE001
            return f"error: fetch failed: {type(exc).__name__}: {exc}"

    return handler


# ---------- backend 1: DuckDuckGo web search ----------

_RESULT_RE = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
    r'.*?<a[^>]*class="result__snippet"[^>]*href="[^"]*"[^>]*>(.*?)</a>',
    re.S,
)

_URL_CLEAN = re.compile(r"^//uddg=([^&]+)")


def _search_web(query: str, max_results: int, urlopen: Callable | None, resolver: Callable | None) -> str:
    data = urllib.parse.urlencode({"q": query}).encode("utf-8")
    page = _open(DDG_ENDPOINT, urlopen=urlopen, resolver=resolver, data=data)
    matches = _RESULT_RE.findall(page)
    if not matches:
        return f"no results for '{query}' (DuckDuckGo may be rate-limiting)"
    lines = []
    for i, (url, title, snippet) in enumerate(matches[:max_results], start=1):
        url = html.unescape(url)
        url = _URL_CLEAN.sub(r"\1", url)  # strip DDG redirect prefix
        url = urllib.parse.unquote(url)
        title = re.sub(r"<[^>]+>", "", html.unescape(title)).strip()
        snippet = re.sub(r"<[^>]+>", "", html.unescape(snippet)).strip()
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return "\n\n".join(lines)


# ---------- backend 2: Google News RSS ----------

def _search_news(query: str, max_results: int, urlopen: Callable | None, resolver: Callable | None) -> str:
    params = urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )
    page = _open(f"{GOOGLE_NEWS_RSS}?{params}", urlopen=urlopen, resolver=resolver)
    try:
        root = ET.fromstring(page)
    except ET.ParseError:
        return f"no news for '{query}' (Google News returned unparseable data)"
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        source = item.find("source")
        source = (source.text or "").strip() if source is not None and source.text else ""
        items.append((title, link, pub, source))
        if len(items) >= max_results:
            break
    if not items:
        return f"no news for '{query}'"
    lines = []
    for i, (title, link, pub, source) in enumerate(items, start=1):
        meta = f"{pub} - {source}".strip(" -")
        lines.append(f"{i}. {title}\n   {meta}\n   {link}")
    return "\n\n".join(lines)


# ---------- backend 3: Wikipedia search ----------

_WIKI_SNIPPET = re.compile(r"<[^>]+>")


def _search_wikipedia(query: str, max_results: int, urlopen: Callable | None, resolver: Callable | None) -> str:
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "srlimit": str(max_results),
            "redirects": "1",
        }
    )
    page = _open(f"{WIKIPEDIA_API}?{params}", urlopen=urlopen, resolver=resolver)
    try:
        payload = json.loads(page)
    except json.JSONDecodeError:
        return f"no results for '{query}' (Wikipedia returned unparseable data)"
    results = (payload.get("query") or {}).get("search") or []
    if not results:
        return f"no results for '{query}' on Wikipedia"
    lines = []
    for i, hit in enumerate(results, start=1):
        title = hit.get("title", "")
        snippet = _WIKI_SNIPPET.sub("", html.unescape(hit.get("snippet") or "")).strip()
        url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return "\n\n".join(lines)


# ---------- fetch_url: read a page as trimmed plain text ----------

_BODY_RE = re.compile(r"<body[^>]*>(.*?)</body>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.S | re.I)
_WS = re.compile(r"\s+")


def _fetch_page(url: str, max_chars: int, urlopen: Callable | None, resolver: Callable | None) -> str:
    page = _open(url, urlopen=urlopen, resolver=resolver)
    body = _BODY_RE.search(page)
    text = body.group(1) if body else page
    text = _SCRIPT_STYLE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS.sub(" ", text).strip()
    if not text:
        return f"no readable text at {url}"
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + " ..."
    return f"Content of {url}:\n\n{text}"
