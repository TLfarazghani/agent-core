"""Parser parity test: web/parser.js must match core/parser.py exactly.

Runs the same corpus of model outputs through both the Python parser and the
JavaScript port (via Node), and asserts identical results -- including the
error-vs-skip semantics of the lenient and strict APIs.

Requires `node` on PATH. Runnable directly (`python test_parser_js.py`) or via
pytest. The JS side is driven through a tiny CommonJS harness string; no extra
files are needed in the repo.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
PARSER_JS = REPO_ROOT / "web" / "parser.js"

from core.parser import (  # noqa: E402
    ParserError,
    has_tool_call_blocks,
    parse_tool_calls,
    parse_tool_calls_strict,
)

# (input, expected call names) -- every entry must round-trip identically.
CORPUS = [
    '<|tool_call_start|>echo(text="hello")<|tool_call_end|>',
    '<|tool_call_start|>echo(text="a")<|tool_call_end|>'
    '<|tool_call_start|>echo(text="b")<|tool_call_end|>',
    "plain answer, no tool call",
    '<|tool_call_start|>echo(text="a")\n<|tool_call_end|>',
    '<|tool_call_start|>web_search(query="liquid ai lfm")<|tool_call_end|>',
    '<|tool_call_start|>run_code(language="python", code="print(1)", timeout_seconds=5)<|tool_call_end|>',
    "<|tool_call_start|>echo(text=\"it's a \\\"quoted\\\" string\")<|tool_call_end|>",
    "<|tool_call_start|>echo(text='single quotes \\' inside')<|tool_call_end|>",
    '<|tool_call_start|>create_docx(title="Report", sections=[{"heading": "Intro", "body": "Hi"}, {"heading": "Body", "body": ["a", "b"]}])<|tool_call_end|>',
    '<|tool_call_start|>create_pptx(title="Deck", slides=[{"title": "S1", "bullets": ["x", "y"]}])<|tool_call_end|>',
    '<|tool_call_start|>echo(count=42, ratio=0.5, flag=True, nothing=None, items=[1, 2, 3])<|tool_call_end|>',
    '<|tool_call_start|>echo(unicode="héllo 世界")<|tool_call_end|>',
    # malformed: lenient skips, strict raises -- both must agree
    "<|tool_call_start|>this is not python(<<<)<|tool_call_end|>",
    "<|tool_call_start|>echo(text=)[bad<|tool_call_end|>",
    "<|tool_call_start|>echo(text=\"unterminated)<|tool_call_end|>",
    # no blocks
    "",
    "just some text with <|tool_call_start|> inside but no close",
]

# Inputs where both parsers must RAISE ParserError in lenient mode too.
ERROR_CORPUS = [
    "<|tool_call_start|>echo('positional')<|tool_call_end|>",
    "<|tool_call_start|>echo(text=other_identifier)<|tool_call_end|>",
    "<|tool_call_start|>echo(text=1 + 2)<|tool_call_end|>",
    "<|tool_call_start|>echo(**kw)<|tool_call_end|>",
    "<|tool_call_start|>obj.method(x=1)<|tool_call_end|>",
]

def run_js(corpus: list[str], errors: list[str]) -> dict:
    parser_path = json.dumps(str(PARSER_JS))
    code = (
        "const fs = require('fs');\n"
        f"const p = require({parser_path});\n"
        "const data = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
        "const out = { lenient: [], strict: [], errors: [], has: [] };\n"
        "for (const t of data.corpus) {\n"
        "  try { out.lenient.push(p.parse_tool_calls(t)); }\n"
        "  catch (e) { out.lenient.push({ __error: e.name }); }\n"
        "  try { out.strict.push(p.parse_tool_calls_strict(t)); }\n"
        "  catch (e) { out.strict.push({ __error: e.name }); }\n"
        "  out.has.push(p.has_tool_call_blocks(t));\n"
        "}\n"
        "for (const t of data.errors) {\n"
        "  try {\n"
        "    const r = p.parse_tool_calls(t);\n"
        "    out.errors.push({ raised: false, result: r });\n"
        "  } catch (e) {\n"
        "    out.errors.push({ raised: e.name === 'ParserError', name: e.name });\n"
        "  }\n"
        "}\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    proc = subprocess.run(
        ["node", "-e", code],
        input=json.dumps({"corpus": corpus, "errors": errors}),
        capture_output=True,
        encoding="utf-8",
        cwd=REPO_ROOT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_parser_parity_lenient() -> None:
    js = run_js(CORPUS, ERROR_CORPUS)
    for i, text in enumerate(CORPUS):
        py = parse_tool_calls(text)
        j = js["lenient"][i]
        assert j != {"__error": "ParserError"}, f"JS raised on corpus[{i}]: {text!r}"
        assert py == j, f"lenient mismatch on corpus[{i}]:\n py={py!r}\n js={j!r}"


def test_parser_parity_strict() -> None:
    js = run_js(CORPUS, ERROR_CORPUS)
    for i, text in enumerate(CORPUS):
        try:
            py = parse_tool_calls_strict(text)
            py_ok = True
            py_err = None
        except ParserError as exc:
            py_ok = False
            py_err = type(exc).__name__
        j = js["strict"][i]
        if isinstance(j, dict) and "__error" in j:
            js_ok = False
            js_err = j["__error"]
        else:
            js_ok = True
            js_err = None
        assert py_ok == js_ok, f"strict ok-mismatch on corpus[{i}]: {text!r} (py {py_err}, js {js_err})"
        if py_ok:
            assert py == j, f"strict result mismatch on corpus[{i}]: {text!r}"


def test_parser_error_corpus() -> None:
    """Inputs with valid syntax but non-literal/positional/attribute calls must
    raise ParserError identically in both parsers."""
    js = run_js(CORPUS, ERROR_CORPUS)
    for i, text in enumerate(ERROR_CORPUS):
        try:
            parse_tool_calls(text)
            raise AssertionError(f"Python lenient did not raise on {text!r}")
        except ParserError:
            pass
        entry = js["errors"][i]
        assert entry["raised"] is True, f"JS did not raise on {text!r}: {entry!r}"


def test_has_tool_call_blocks() -> None:
    js = run_js(CORPUS, ERROR_CORPUS)
    for i, text in enumerate(CORPUS):
        py = has_tool_call_blocks(text)
        assert py == js["has"][i], f"has_tool_call_blocks mismatch on corpus[{i}]: {text!r}"


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
    print(f"\nAll {len(tests)} parser-parity tests passed.")


if __name__ == "__main__":
    main()