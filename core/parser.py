"""Tool-call parser: <|tool_call_start|>...<|tool_call_end|> -> list[dict].

Single source of truth for extracting tool calls from model output.
Stdlib-only on purpose: it ports line-for-line to parser.js (WebGPU) and
AgentCore.kt (Android) and must never gain third-party imports.

LFM2.5 emits native Pythonic calls, e.g.

    <|tool_call_start|>web_search(query="liquid ai lfm")<|tool_call_end|>

Arguments are extracted with Python's ``ast`` module and evaluated only as
literals via ``ast.literal_eval`` -- never ``eval``.

Returns a list of plain dicts ``{"id", "name", "arguments"}`` so the parser
stays portable; the caller validates them into ``state.ToolCall``.
"""

from __future__ import annotations

import ast
import re
from typing import Any

TOOL_CALL_START = "<|tool_call_start|>"
TOOL_CALL_END = "<|tool_call_end|>"

_BLOCK_RE = re.compile(
    re.escape(TOOL_CALL_START) + r"(.*?)" + re.escape(TOOL_CALL_END),
    re.DOTALL,
)


class ParserError(ValueError):
    pass


def extract_blocks(text: str) -> list[str]:
    """Return the raw text between each sentinel pair, stripped."""
    return [m.group(1).strip() for m in _BLOCK_RE.finditer(text)]


def _func_name(expr: ast.expr) -> str:
    if isinstance(expr, ast.Name):
        return expr.id
    raise ParserError(f"unsupported call target: {ast.dump(expr)}")


def _literal_value(node: ast.expr) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError) as exc:
        raise ParserError(f"non-literal argument: {ast.dump(node)}") from exc


def _parse_call(node: ast.Call, index: int) -> dict:
    if node.args:
        raise ParserError(
            f"positional arguments are not supported; use keyword arguments: {ast.dump(node)}"
        )
    args: dict = {}
    for kw in node.keywords:
        if kw.arg is None:
            raise ParserError(f"unexpected **kwargs in tool call: {ast.dump(node)}")
        args[kw.arg] = _literal_value(kw.value)
    return {
        "id": f"call_{index + 1:04d}",
        "name": _func_name(node.func),
        "arguments": args,
    }


def parse_tool_calls(text: str) -> list[dict]:
    """Extract every tool call from ``text`` as a dict.

    Lenient mode: blocks that fail to *parse* (syntax errors) are skipped.
    Semantic failures are NOT skipped — a block that parses but is not a
    valid tool call (positional args, ``**kwargs``, non-literal argument
    values) raises ``ParserError``. That mirrors ``ParserSyntaxError`` /
    ``ParserError`` in web/parser.js. Callers that want full leniency should
    wrap this and treat ``ParserError`` as "drop the block". The caller can
    detect partial output by comparing against ``extract_blocks``.
    """
    calls: list[dict] = []
    index = 0
    for block in extract_blocks(text):
        try:
            tree = ast.parse(block)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                calls.append(_parse_call(node, index))
                index += 1
    return calls


def parse_tool_calls_strict(text: str) -> list[dict]:
    """Like ``parse_tool_calls`` but raises ``ParserError`` on malformed input."""
    calls: list[dict] = []
    for block in extract_blocks(text):
        try:
            tree = ast.parse(block)
        except SyntaxError as exc:
            raise ParserError(f"malformed tool-call block: {block!r}") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                calls.append(_parse_call(node, len(calls)))
    return calls


def has_tool_call_blocks(text: str) -> bool:
    return bool(_BLOCK_RE.search(text))