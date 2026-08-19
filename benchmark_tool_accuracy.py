r"""Phase 7 live benchmark: tool-call accuracy + agent-turn tok/s vs the 1.2B
baseline (docs/benchmarks.md §1-2).

Runs scripted prompts through the real Windows path (core.loop -> orchestrator
-> llama-server --jinja) against whichever model the server is serving, N
trials each, and prints a side-by-side table. Requires llama-server up with the
target model (set AGENT_CORE_MODEL before launching server_config.ps1).

--registry minimal (default) replicates the 1.2B baseline's exact tool set
(create_docx/create_pptx/run_code only, no web_search/cognitive) so the
accuracy numbers are apples-to-apples; --registry full adds web_search +
cognitive tools to see the model's real-world tool preference.

Usage: .venv\Scripts\python benchmark_tool_accuracy.py [--trials N] [--registry minimal|full]
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import ChatMessage, ToolRegistry, user_message
from core.meta import agent_bio
from core.sessions import new_agent_state
from core.loop import step
from windows.orchestrator import LlamaCppProvider

# prompt -> (expected tool name or None, expected-substring matcher)
PROMPTS = [
    ("create_docx (well-specified)", "make a docx titled Quarterly Report",
     "create_docx", lambda a: isinstance(a.get("title"), str) and "report" in a["title"].lower() and a.get("sections")),
    ("create_docx (under-specified)", "meeting notes",
     "create_docx", lambda a: isinstance(a.get("title"), str) and bool(a.get("sections"))),
    ("create_pptx", "create a powerpoint about project status with 3 slides",
     "create_pptx", lambda a: isinstance(a.get("title"), str) and bool(a.get("slides"))),
    ("run_code", "run python code that prints 42",
     "run_code", lambda a: a.get("language") == "python" and "print" in a.get("code", "")),
    ("none (answer directly)", "what is the capital of France",
     None, lambda a: True),
]


def make_registry(kind: str) -> ToolRegistry:
    from tools import register_docgen_tools, register_runcode_tool

    registry = ToolRegistry()
    register_docgen_tools(registry)
    register_runcode_tool(registry)
    if kind == "full":
        from tools import register_cognitive_tools, register_web_tools

        register_web_tools(registry)
        register_cognitive_tools(registry)
    return registry


def run_trial(prompt: str, registry: ToolRegistry) -> dict:
    state = new_agent_state()
    state.messages.append(ChatMessage(role="system", content=agent_bio(state, registry)))
    state.messages.append(user_message(prompt))
    provider = LlamaCppProvider(registry=registry, stream=False)
    step(state, provider, registry)

    calls = []
    for m in state.messages:
        if m.role == "assistant" and m.function_calls:
            calls.extend((c.name, c.arguments) for c in m.function_calls)
    call_names = [name for name, _ in calls]

    # run_code parks at the approval gate (never executes) -- that IS the correct behavior.
    pending = state.pending_approval
    if pending is not None:
        call_names.append(pending.tool_name)

    return {
        "call_names": call_names,
        "args": calls[0][1] if calls else {},
        "prompt_tokens": provider.last_usage.get("prompt_tokens", 0),
        "completion_tokens": provider.last_usage.get("completion_tokens", 0),
        "latency_s": provider.last_latency_s,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--registry", choices=["minimal", "full"], default="minimal")
    args = parser.parse_args()

    registry = make_registry(args.registry)
    tool_names = sorted(d.name for d in registry.definitions())
    print(f"Benchmarking live llama-server, {args.trials} trials/prompt, tools: {tool_names}\n")
    rows = []
    for label, prompt, expected_name, args_ok in PROMPTS:
        correct = 0
        made_call = 0
        tokens = 0
        latency = 0.0
        samples = []
        for _ in range(args.trials):
            r = run_trial(prompt, registry)
            tokens += r["completion_tokens"]
            latency += r["latency_s"]
            called = r["call_names"]
            hit = bool(called) and called[0] == expected_name if expected_name else not called
            if hit:
                made_call += 1
                if expected_name is None or args_ok(r["args"]):
                    correct += 1
                    samples.append("ok")
                else:
                    samples.append("bad-args")
            else:
                samples.append("wrong-call" if called else "no-call")
        pct = round(100.0 * correct / args.trials, 1)
        tok_s = round(tokens / latency, 1) if latency else 0.0
        rows.append((label, correct, made_call, pct, tok_s, samples))
        print(f"{label:<32} {correct}/{args.trials} correct ({pct}%)  ~{tok_s} tok/s  {samples}")

    total_correct = sum(r[1] for r in rows)
    total = args.trials * len(PROMPTS)
    print(f"\nTOTAL: {total_correct}/{total} = {round(100.0 * total_correct / total, 1)}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())