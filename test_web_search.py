"""Web search tool smoke test (real code, fake transport).

Supplies a fake ``urlopen`` AND a fake ``resolver`` so no network or live DNS
is touched: verifies DDG web search parsing, Google News RSS parsing,
Wikipedia JSON parsing, fetch_url text extraction, argument validation, the
SSRF destination guard (private/loopback/link-local/metadata blocked after DNS
resolution, every redirect re-checked, byte cap), and registry wiring.
Runnable directly or via pytest.
"""

from __future__ import annotations

from core import AgentState, ChatMessage, ToolCall, ToolRegistry, new_state
from tools import register_web_tools
from tools.web_search import MAX_REDIRECTS, RESPONSE_BYTE_CAP

# Public stand-in A record (example.com's address); external, not excluded.
PUBLIC_A = ("93.184.216.34", 80)


def _public_resolver(host, port):
    """Fake getaddrinfo(host, port): loopback for localhost, private for the
    ``intranet`` fixture namespace, external otherwise."""
    if host == "localhost" or host.startswith("intranet"):
        ip = "127.0.0.1" if host == "localhost" else "10.0.0.5"
        return [(2, 1, 6, "", (ip, port or 0))]
    return [(2, 1, 6, "", PUBLIC_A)]


def _failing_resolver(host, port):
    raise OSError(f"DNS lookup failed for {host}")


def _empty_resolver(host, port):
    return []


DDG_PAGE = """
<html><body>
<div class="result">
  <h2 class="result__title"><a class="result__a"
      href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.liquid.ai%2Fblog%2Fintro-lfm2">
      Introducing LFM2</a></h2>
  <a class="result__snippet" href="https://www.liquid.ai/blog/intro-lfm2">
      A new class of <b>foundation models</b>.</a>
</div>
<div class="result">
  <h2 class="result__title"><a class="result__a" href="https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF">
      LFM2.5 GGUF on Hugging Face</a></h2>
  <a class="result__snippet" href="https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF">
      Official GGUF weights.</a>
</div>
</body></html>
"""

GOOGLE_NEWS_PAGE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Google News</title>
<item><title>Liquid AI ships LFM2.5</title>
<link>https://news.google.com/rss/articles/ABC</link>
<pubDate>Tue, 18 Aug 2026 10:00:00 GMT</pubDate>
<source url="https://example.com">VentureBeat</source>
</item>
<item><title>LFM2.5 runs on a Raspberry Pi</title>
<link>https://news.google.com/rss/articles/DEF</link>
<pubDate>Tue, 18 Aug 2026 09:00:00 GMT</pubDate>
<source url="https://example.com">TechCrunch</source>
</item>
</channel></rss>
"""

WIKI_PAGE = (
    '{"query": {"search": ['
    '{"title": "Liquid AI", "snippet": "<span class=\\"searchmatch\\">Liquid</span> AI is a company building foundation models"},'
    '{"title": "LFM", "snippet": "Linearized foundation models"}]}}'
)

HTML_PAGE = (
    "<html><head><title>Example</title></head><body>"
    "<h1>Welcome</h1><p>This is <b>readable</b> content for a test.</p>"
    "<script>var x = 1;</script></body></html>"
)


class _Headers(dict):
    """Minimal headers mapping F.U.N. compatible with .get()."""


class _Response:
    """Fake transport response: single-shot .read, optional status/headers."""

    def __init__(self, data: bytes = b"", status: int | None = None, headers: dict | None = None):
        self._data = data
        self._read_done = False
        self.status = status
        self.headers = _Headers(headers or {})

    def read(self, *args) -> bytes:
        if self._read_done:
            return b""
        self._read_done = True
        return self._data

    def getcode(self):
        return self.status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc) -> None:
        return None


class FakeUrlopen:
    """urlopen(request, timeout) -> response.

    With no ``script`` set, returns 200 + ``_data`` (the classic canned page).
    With ``script``, pops one response per call so redirects can be rehearsed.
    Records every (full_url, data) call so tests can assert that blocked
    destinations never reach the transport.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes | None]] = []
        self._script: list[_Response] = []

    def set_data(self, data: str) -> None:
        self._script = [_Response(data.encode("utf-8"), status=200)]

    def set_script(self, responses: list[_Response]) -> None:
        self._script = list(responses)

    def __call__(self, request, timeout):
        data = None if request.data is None else request.data
        self.calls.append((request.full_url, data))
        if not self._script:
            raise AssertionError("FakeUrlopen called with no scripted response")
        return self._script.pop(0)


def _dispatch(registry: ToolRegistry, name: str, args: dict) -> ChatMessage:
    return registry.dispatch(
        new_state(target="windows", model="LFM2.5-1.2B-Instruct"),
        ToolCall(id="call_0001", name=name, arguments=args),
    )


def _web_registry(fake: FakeUrlopen, resolver=_public_resolver) -> ToolRegistry:
    registry = ToolRegistry()
    register_web_tools(registry, urlopen=fake, resolver=resolver)
    return registry


# ---------- parsing backends (existing coverage, resolver-seamed) ----------

def test_web_search_general_parses_ddg() -> None:
    fake = FakeUrlopen()
    fake.set_data(DDG_PAGE)
    msg = _dispatch(_web_registry(fake), "web_search", {"query": "liquid ai lfm2"})
    assert msg.role == "tool"
    assert "Introducing LFM2" in msg.content
    assert "huggingface.co/LiquidAI" in msg.content
    assert "foundation models" in msg.content
    url, data = fake.calls[0]
    assert url.startswith("https://html.duckduckgo.com/html/")
    assert b"q=liquid+ai+lfm2" in data


def test_web_search_news_parses_rss() -> None:
    fake = FakeUrlopen()
    fake.set_data(GOOGLE_NEWS_PAGE)
    msg = _dispatch(_web_registry(fake), "web_search", {"query": "liquid ai", "kind": "news"})
    assert msg.role == "tool"
    assert "Liquid AI ships LFM2.5" in msg.content
    assert "VentureBeat" in msg.content
    assert "news.google.com/rss/articles/ABC" in msg.content


def test_web_search_wikipedia_parses_json() -> None:
    fake = FakeUrlopen()
    fake.set_data(WIKI_PAGE)
    msg = _dispatch(_web_registry(fake), "web_search", {"query": "liquid ai", "kind": "wikipedia"})
    assert msg.role == "tool"
    assert "Liquid AI" in msg.content
    assert "en.wikipedia.org/wiki/Liquid_AI" in msg.content


def test_web_search_unknown_kind() -> None:
    fake = FakeUrlopen()
    msg = _dispatch(_web_registry(fake), "web_search", {"query": "q", "kind": "carrier_pigeon"})
    assert msg.role == "tool"
    assert "invalid arguments" in msg.content
    assert fake.calls == []


def test_web_search_max_results_bounds() -> None:
    fake = FakeUrlopen()
    msg = _dispatch(_web_registry(fake), "web_search", {"query": "q", "max_results": 99})
    assert msg.role == "tool"
    assert "invalid arguments" in msg.content


# ---------- fetch_url text extraction ----------

def test_fetch_url_extracts_text() -> None:
    fake = FakeUrlopen()
    fake.set_data(HTML_PAGE)
    msg = _dispatch(_web_registry(fake), "fetch_url", {"url": "https://example.com/page"})
    assert msg.role == "tool"
    assert "readable content" in msg.content
    assert "<b>" not in msg.content
    assert "var x = 1" not in msg.content  # <script> stripped


def test_fetch_url_rejects_bad_scheme() -> None:
    fake = FakeUrlopen()
    msg = _dispatch(_web_registry(fake), "fetch_url", {"url": "file:///etc/passwd"})
    assert msg.role == "tool"
    assert "must start with http" in msg.content
    assert fake.calls == []


def test_fetch_url_text_cap() -> None:
    fake = FakeUrlopen()
    fake.set_data("<html><body>" + ("word " * 8000) + "</body></html>")
    msg = _dispatch(_web_registry(fake), "fetch_url", {"url": "https://example.com/long", "max_chars": 500})
    assert msg.role == "tool"
    assert len(msg.content) < 1200  # returned text is trimmed well under the body size
    assert msg.content.endswith("...")


def test_fetch_url_byte_cap() -> None:
    fake = FakeUrlopen()
    fake.set_data("<html><body>" + ("x" * (RESPONSE_BYTE_CAP + 10)) + "</body></html>")
    msg = _dispatch(_web_registry(fake), "fetch_url", {"url": "https://example.com/huge"})
    assert msg.role == "tool"
    assert "byte download cap" in msg.content


# ---------- SSRF guard: destinations blocked after DNS resolution ----------

def test_fetch_url_dns_resolved_private_blocked() -> None:
    def private_resolver(host, port):
        return [(2, 1, 6, "", ("10.0.0.5", port or 0))]

    fake = FakeUrlopen()
    fake.set_data(HTML_PAGE)  # would succeed if the transport was ever reached
    msg = _dispatch(_web_registry(fake, resolver=private_resolver), "fetch_url", {"url": "https://example.com/x"})
    assert "blocked destination" in msg.content
    assert "private address" in msg.content
    assert fake.calls == []  # guard ran BEFORE the transport


def test_fetch_url_dns_failure_blocked() -> None:
    fake = FakeUrlopen()
    msg = _dispatch(_web_registry(fake, resolver=_failing_resolver), "fetch_url", {"url": "https://no-such-host.invalid/x"})
    assert msg.role == "tool"
    assert "DNS resolution failed" in msg.content
    assert fake.calls == []


def test_fetch_url_no_records_blocked() -> None:
    fake = FakeUrlopen()
    msg = _dispatch(_web_registry(fake, resolver=_empty_resolver), "fetch_url", {"url": "https://example.com/x"})
    assert "resolved to no addresses" in msg.content
    assert fake.calls == []


def test_fetch_url_ip_literals_blocked() -> None:
    cases = [
        ("http://127.0.0.1:8001/health", "loopback"),
        ("https://10.1.2.3/admin", "private address"),
        ("https://192.168.1.5/router", "private address"),
        ("http://172.16.0.9:8080/", "private address"),
        ("http://169.254.169.254/latest/meta-data/", "metadata service"),
        ("http://[::1]/", "loopback"),
        ("http://[fd00:ec2::254]/", "metadata service"),
        ("http://100.100.100.200/sts", "metadata service" if False else "metadata service"),
    ]
    for url, expected_reason in cases:
        fake = FakeUrlopen()
        registry = _web_registry(fake)  # public resolver: literal IPs short-circuit DNS
        msg = _dispatch(registry, "fetch_url", {"url": url})
        assert "blocked destination" in msg.content, url
        assert expected_reason in msg.content, url
        assert fake.calls == [], url


def test_web_search_backend_endpoint_guarded() -> None:
    def internal_resolver(host, port):
        return [(2, 1, 6, "", ("127.0.0.1", port or 0))]

    fake = FakeUrlopen()
    msg = _dispatch(_web_registry(fake, resolver=internal_resolver), "web_search", {"query": "q"})
    assert "blocked destination" in msg.content
    assert fake.calls == []


def test_fetch_url_localhost_name_blocked() -> None:
    # "localhost" skips DNS via the resolver returning loopback anyway.
    fake = FakeUrlopen()
    fake.set_data(HTML_PAGE)
    msg = _dispatch(_web_registry(fake), "fetch_url", {"url": "http://localhost:8001/health"})
    assert "loopback" in msg.content
    assert fake.calls == []


# ---------- SSRF guard: every redirect target re-checked ----------

def _redirect(status: int, location: str) -> _Response:
    return _Response(b"", status=status, headers={"Location": location})


def test_redirect_to_internal_blocked_before_transport() -> None:
    fake = FakeUrlopen()
    fake.set_script([_redirect(302, "http://127.0.0.1:8001/v1/models")])
    msg = _dispatch(_web_registry(fake), "fetch_url", {"url": "https://example.com/redirect"})
    assert "blocked destination" in msg.content
    assert "loopback" in msg.content
    # Only the FIRST hop reached the transport; the internal target was
    # rejected by the guard before any connection attempt.
    assert len(fake.calls) == 1
    assert fake.calls[0][0] == "https://example.com/redirect"


def test_redirect_chain_public_allowed() -> None:
    fake = FakeUrlopen()
    fake.set_script(
        [
            _redirect(302, "https://example.com/other"),
            _redirect(303, "/final"),
            _Response(HTML_PAGE.encode("utf-8"), status=200),
        ]
    )
    msg = _dispatch(_web_registry(fake), "fetch_url", {"url": "https://example.com/start"})
    assert msg.role == "tool"
    assert "readable content" in msg.content
    assert len(fake.calls) == 3
    assert fake.calls[-1][0] == "https://example.com/final"


def test_redirect_s_relative_location_joined() -> None:
    fake = FakeUrlopen()
    fake.set_script(
        [
            _redirect(301, "other"),
            _Response(HTML_PAGE.encode("utf-8"), status=200),
        ]
    )
    msg = _dispatch(_web_registry(fake), "fetch_url", {"url": "https://example.com/a/b"})
    assert "readable content" in msg.content
    assert fake.calls[1][0] == "https://example.com/a/other"


def test_redirect_to_bad_scheme_blocked() -> None:
    fake = FakeUrlopen()
    fake.set_script([_redirect(302, "file:///etc/passwd")])
    msg = _dispatch(_web_registry(fake), "fetch_url", {"url": "https://example.com/redir"})
    assert "blocked destination" in msg.content
    assert "not http/https" in msg.content
    assert len(fake.calls) == 1


def test_redirect_hops_capped() -> None:
    fake = FakeUrlopen()
    hops = MAX_REDIRECTS + 1
    fake.set_script([_redirect(302, f"https://example.com/h{i}") for i in range(hops)])
    msg = _dispatch(_web_registry(fake), "fetch_url", {"url": "https://example.com/loop"})
    assert "more than" in msg.content and "redirects" in msg.content
    assert len(fake.calls) == MAX_REDIRECTS  # 5 opens; the 6th target is refused


# ---------- registry wiring ----------

def test_definitions_loaded_with_schemas() -> None:
    fake = FakeUrlopen()
    registry = _web_registry(fake)
    names = {d.name for d in registry.definitions()}
    assert names == {"web_search", "fetch_url"}
    ws = next(d for d in registry.definitions() if d.name == "web_search")
    assert ws.requires_approval is False
    assert {"web", "news", "wikipedia"} <= set(ws.parameters["properties"]["kind"]["enum"])


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {test.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        raise SystemExit(f"{failures} test(s) failed")
    print(f"\nAll {len(tests)} web-search tool tests passed.")


if __name__ == "__main__":
    main()
