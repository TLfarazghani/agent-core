"""web_search + fetch_url handlers — real, keyless web search (stdlib only).

Three no-key backends (research doc: web search must work without an external
MCP remote):

- ``kind="web"``       -> DuckDuckGo HTML search (POST /html/), parse top results
- ``kind="news"``      -> Google News RSS (news.google.com/rss/search), latest headlines
- ``kind="wikipedia"`` -> Wikipedia Search API (JSON), encyclopedia entries

``fetch_url`` downloads a page and returns a trimmed plain-text extract for
reading content that a search snippet can't convey.

All network calls go through an injectable ``urlopen`` so tests can supply a
fake without touching the network (mirrors run_code's injectable docker client).
The module stays stdlib-only so it ports to the browser worker (which proxies
through web/server.py's /api/search to avoid CORS).

Schema (registry.json): ``web_search(query, kind?, max_results?)`` and
``fetch_url(url, max_chars?)``. Neither requires approval (read-only).
"""

from __future__ import annotations

import html
import json
import re
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


def make_handler(urlopen: Callable | None = None) -> Callable[[dict[str, Any]], str]:
    """Return a ToolRegistry handler that performs live web search.

    ``urlopen`` mirrors ``urllib.request.urlopen`` (request, timeout) -> file-like.
    When omitted, the real stdlib opener is used.
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
                return _search_web(query, max_results, urlopen)
            if kind == "news":
                return _search_news(query, max_results, urlopen)
            return _search_wikipedia(query, max_results, urlopen)
        except Exception as exc:  # noqa: BLE001
            return f"error: web search failed: {type(exc).__name__}: {exc}"

    return handler


def make_fetch_handler(urlopen: Callable | None = None) -> Callable[[dict[str, Any]], str]:
    def handler(arguments: dict[str, Any]) -> str:
        url = arguments.get("url", "")
        if not url.startswith(("http://", "https://")):
            return "error: url must start with http:// or https://"
        max_chars = int(arguments.get("max_chars", 4000))
        if max_chars < 200 or max_chars > 20000:
            return "error: max_chars must be between 200 and 20000"
        try:
            return _fetch_page(url, max_chars, urlopen)
        except Exception as exc:  # noqa: BLE001
            return f"error: fetch failed: {type(exc).__name__}: {exc}"

    return handler


# ---------- low-level transport ----------

def _open(urlopen: Callable | None, url: str, data: bytes | None = None) -> str:
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    opener = urlopen or urllib.request.urlopen
    with opener(request, timeout=15) as resp:
        return resp.read().decode("utf-8", "replace")


# ---------- backend 1: DuckDuckGo web search ----------

_RESULT_RE = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
    r'.*?<a[^>]*class="result__snippet"[^>]*href="[^"]*"[^>]*>(.*?)</a>',
    re.S,
)

_URL_CLEAN = re.compile(r"^//uddg=([^&]+)")


def _search_web(query: str, max_results: int, urlopen: Callable | None) -> str:
    data = urllib.parse.urlencode({"q": query}).encode("utf-8")
    page = _open(urlopen, DDG_ENDPOINT, data=data)
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

def _search_news(query: str, max_results: int, urlopen: Callable | None) -> str:
    params = urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )
    page = _open(urlopen, f"{GOOGLE_NEWS_RSS}?{params}")
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


def _search_wikipedia(query: str, max_results: int, urlopen: Callable | None) -> str:
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
    page = _open(urlopen, f"{WIKIPEDIA_API}?{params}")
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


def _fetch_page(url: str, max_chars: int, urlopen: Callable | None) -> str:
    page = _open(urlopen, url)
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
