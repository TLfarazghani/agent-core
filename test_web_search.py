"""Web search tool smoke test (real code, fake transport).

Supplies a fake ``urlopen`` so no network is touched: verifies DDG web search
parsing, Google News RSS parsing, Wikipedia JSON parsing, fetch_url text
extraction, argument validation, and registry wiring. Runnable directly or via
pytest.
"""

from __future__ import annotations

from core import AgentState, ChatMessage, ToolCall, ToolRegistry, new_state
from tools import register_web_tools

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


class FakeUrlopen:
    """urlopen(request, timeout) -> object with .read() that returns canned data."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes | None]] = []
        self._data = DDG_PAGE

    def set_data(self, data: str) -> None:
        self._data = data

    def __call__(self, request, timeout):
        data = None if request.data is None else request.data
        self.calls.append((request.full_url, data))
        return _FakeResponse(self._data)


class _FakeResponse:
    def __init__(self, data: str) -> None:
        self._data = data.encode("utf-8")

    def read(self, *args) -> bytes:
        return self._data

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> None:
        return None


def _dispatch(registry: ToolRegistry, name: str, args: dict) -> ChatMessage:
    return registry.dispatch(
        new_state(target="windows", model="LFM2.5-1.2B-Instruct"),
        ToolCall(id="call_0001", name=name, arguments=args),
    )


def test_web_search_general_parses_ddg() -> None:
    fake = FakeUrlopen()
    registry = ToolRegistry()
    register_web_tools(registry, urlopen=fake)
    msg = _dispatch(registry, "web_search", {"query": "liquid ai lfm2"})
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
    registry = ToolRegistry()
    register_web_tools(registry, urlopen=fake)
    msg = _dispatch(registry, "web_search", {"query": "liquid ai", "kind": "news"})
    assert msg.role == "tool"
    assert "Liquid AI ships LFM2.5" in msg.content
    assert "VentureBeat" in msg.content
    assert "news.google.com/rss/articles/ABC" in msg.content


def test_web_search_wikipedia_parses_json() -> None:
    fake = FakeUrlopen()
    fake.set_data(WIKI_PAGE)
    registry = ToolRegistry()
    register_web_tools(registry, urlopen=fake)
    msg = _dispatch(registry, "web_search", {"query": "liquid ai", "kind": "wikipedia"})
    assert msg.role == "tool"
    assert "Liquid AI" in msg.content
    assert "en.wikipedia.org/wiki/Liquid_AI" in msg.content


def test_web_search_unknown_kind() -> None:
    fake = FakeUrlopen()
    registry = ToolRegistry()
    register_web_tools(registry, urlopen=fake)
    msg = _dispatch(registry, "web_search", {"query": "q", "kind": "carrier_pigeon"})
    assert msg.role == "tool"
    assert "invalid arguments" in msg.content
    assert fake.calls == []


def test_web_search_max_results_bounds() -> None:
    fake = FakeUrlopen()
    registry = ToolRegistry()
    register_web_tools(registry, urlopen=fake)
    msg = _dispatch(registry, "web_search", {"query": "q", "max_results": 99})
    assert msg.role == "tool"
    assert "invalid arguments" in msg.content


def test_fetch_url_extracts_text() -> None:
    fake = FakeUrlopen()
    fake.set_data(HTML_PAGE)
    registry = ToolRegistry()
    register_web_tools(registry, urlopen=fake)
    msg = _dispatch(registry, "fetch_url", {"url": "https://example.com/page"})
    assert msg.role == "tool"
    assert "readable content" in msg.content
    assert "<b>" not in msg.content
    assert "var x = 1" not in msg.content  # <script> stripped


def test_fetch_url_rejects_bad_scheme() -> None:
    fake = FakeUrlopen()
    registry = ToolRegistry()
    register_web_tools(registry, urlopen=fake)
    msg = _dispatch(registry, "fetch_url", {"url": "file:///etc/passwd"})
    assert msg.role == "tool"
    assert "must start with http" in msg.content
    assert fake.calls == []


def test_definitions_loaded_with_schemas() -> None:
    fake = FakeUrlopen()
    registry = ToolRegistry()
    register_web_tools(registry, urlopen=fake)
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
