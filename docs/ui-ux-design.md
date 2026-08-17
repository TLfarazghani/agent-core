# UI / UX Design

Design spec for the agent-core surfaces: the interactive CLI (current), the Web UI (primary long-term surface), and Android (planned). Everything here maps back to the shared core contracts in `docs/data-contracts.md` — the UI never owns agent state; it renders it and resolves approvals.

## Design principles

1. **Local-first and private.** The interface must make clear the agent runs on-device. No cloud chrome, no account walls, no telemetry affordances. Offline is a feature to communicate, not a fallback.
2. **Total transparency of tool use.** Every tool call the agent makes is visible as a first-class UI element — never hidden. If the agent reads a file or searches the web, the user sees the card with its arguments.
3. **Human-in-the-loop is the product.** The approval gate (`run_code` and any `requires_approval` tool) is the defining interaction of this assistant. The approval UI must be unmissable, calm, and reversible. It is enforced in `core/tool_registry.dispatch()`; the UI merely surfaces `AgentState.pending_approval`.
4. **Streaming over waiting.** Generation and tool execution stream incrementally. A local 1.2B model is fast but not instant — show progress, never a frozen spinner.
5. **Session continuity.** `AgentState` (session_id, messages, turn_count, max_turns) is serializable. Resuming a session must be one command/one click. Losing a session is a bug.
6. **Conservative by default.** The wrong default is the one that executes code. Approval defaults to *show*, never auto-approve, never auto-reject silently.

## Platform coverage matrix

| Surface | Status | Primary interactions |
|---|---|---|
| **CLI (Windows)** | **Shipped 2026-08-17** (`cli.py`) | REPL, streaming text, tool-call cards as text, approval prompt |
| **Web UI** | Planned (with WebGPU port) | Full chat canvas, tool-call cards, approval modal, session list |
| **Android** | Planned port | Chat view, notification-style approvals, no `run_code` |

All three render the same state. This spec defines the Web UI as the reference; CLI and Android are constrained simplifications of it.

---

## 1. CLI UX (Windows, immediate)

The CLI is the first shipped surface. It must be usable by someone who has never seen the tool, and scriptable enough for the library API (`core.loop.step/run`).

### 1.1 Layout

```
$ agent
┌─ agent-core · LFM2.5-1.2B-Instruct · local ─────────────┐
│  session 9f3a2c  ·  turn 3/8  ·  model on-device        │
└──────────────────────────────────────────────────────────┘

> summarize the meeting notes in notes.md

 ⟦ tool: read_file ⟧
   path = "notes.md"
 ⟦ done: 42 lines ⟧

 assistant › Here's a summary: …

> run this: for i in range(10): print(i)

 ⚠ PENDING APPROVAL — run_code
   language = python
   code     = for i in range(10): print(i)
   Approve? [y/N] 
```

### 1.2 Behavior rules

- **Streaming:** assistant text prints token-by-token; no buffered wall of text.
- **Tool calls** render as an indented `⟦ tool ⟧` block with the parsed arguments, then a `⟦ done ⟧`/`⟦ error ⟧` line with the tool result. Uses `core.parser` output — the same shape the Web UI renders.
- **Approval prompt:** hard stop on `state.pending_approval`. Prompt shows tool name, language, and code; default answer is **N**. Empty input = N. `y`/`yes` executes; `n`/`no`/anything else rejects and clears state via `resolve_approval(approved=False)`.
- **Turn cap:** when `turn_count >= max_turns`, print a clear terminal message ("Turn budget reached. Start a new session with /new") instead of failing silently.
- **No ANSI when piped:** disable color/emoji when stdout is not a TTY so `agent | tee log` stays clean.

### 1.3 Commands

| Command | Behavior |
|---|---|
| `agent` | Start REPL in a new session |
| `/new` | Start a new session (fresh `AgentState`) |
| `/resume <id>` | Load session by id |
| `/tools` | List registered tools (names + one-line descriptions) |
| `/approve` / `/reject` | Resolve the current pending approval |
| `/quit` | Persist session, exit |
| `Ctrl-C` | Stop generation; does **not** discard the session |

### 1.4 Exit codes

| Code | Meaning |
|---|---|
| 0 | Clean exit (session persisted) |
| 1 | Fatal config/model error (llama-server unreachable) |
| 2 | Approval rejected → still 0 semantically; keep for tooling |

---

## 2. Web UI (reference design)

### 2.1 Layout

```
┌────────────────────────────────────────────────────────────────┐
│ sidebar · sessions         │  chat canvas                       │
│ ─────────────────────      │  ┌──────────────────────────────┐  │
│  today                    │  │ [user bubble]                │  │
│   • summarize notes  ●     │  │                              │  │
│   • draft pptx            │  │ [tool-card] read_file        │  │
│  this week                │  │ ┌──────────────────────────┐ │  │
│   • email followup        │  │ │ ⟦ read_file ⟧            │ │  │
│ ─────────────────────      │  │ │ path = notes.md          │ │  │
│  [ + New session ]        │  │ └──────────────────────────┘ │  │
│                            │  │ [assistant streaming]      │  │
│  model  ● LFM2.5 1.2B     │  │                              │  │
│  status ● local · offline  │  │ ┌── approval card ────────┐  │  │
└────────────────────────────┘  │ │ ⚠ run_code             │  │  │
                               │ │ approve  · reject      │  │  │
                               │ └────────────────────────┘  │  │
                               │  [ input box ▸ ]            │  │
                               └──────────────────────────────┘  │
```

### 2.2 Components

| Component | Purpose | State derived from |
|---|---|---|
| **SessionSidebar** | List/resume sessions, new-session, delete | `AgentState.session_id` + local index |
| **ModelStatusBar** | Model name, target, online/offline, context usage | Provider health (e.g. `llama-server /health`), `AgentState` |
| **MessageBubble** | One `ChatMessage`; role styling (user/assistant/tool/system) | `state.messages[]` |
| **ToolCallCard** | Collapsible card: tool name, parsed arguments (pretty JSON), status (pending/running/done/error), runtime | `ChatMessage.function_calls[]` + tool result message |
| **ApprovalCard** | Inline or modal: high-risk tool payload, Approve/Reject with optional rationale | `AgentState.pending_approval` (non-null) |
| **StreamingText** | Incremental render of assistant content while generating | provider stream |
| **Composer** | Input, send, stop-generation, turn counter (`turn_count/max_turns`) | `AgentState` |

### 2.3 Interaction flows

**Normal turn (no approval):**
1. User sends message → `run(state)`.
2. Assistant text streams into `StreamingText`.
3. If the assistant emits tool calls, they appear as `ToolCallCard`s in order; results render inside each card when the tool result message arrives.
4. The turn ends when the assistant returns a final text-only message (terminal state per `core.loop.run`).

**Approval flow (the critical path):**
1. `run_code` (or any `requires_approval` tool) is dispatched → `state.pending_approval` set → loop halts.
2. The **ApprovalCard** slides in above the composer; generation indicator stops; composer is locked to "approve/reject" only.
3. Card shows: tool name, language, full code (scrollable, monospace, syntax-highlighted), and a one-line explanation of what will run.
4. **Approve** → `resolve_approval(approved=True)`; the tool card transitions to "running → done" with its result.
5. **Reject** → `resolve_approval(approved=False)`; the card collapses to a muted "Rejected" state; the conversation continues normally.
6. Safety: **Enter does not approve.** Approve requires an explicit click/tap (or `Ctrl+Enter` when focus is on the approve button). Keyboard shortcut `a` = approve, `r` = reject, visible in the card.

**Turn cap:**
- When `turn_count` reaches `max_turns`, the composer shows "Turn budget reached" and offers **New session**. The history stays viewable.

### 2.4 States

| State | Visual | Behavior |
|---|---|---|
| `pending_approval == null`, generating | streaming text, running indicator | composer disabled, stop button visible |
| `pending_approval != null` | ApprovalCard, composer locked | only approve/reject active |
| terminal answer | normal messages | composer re-enabled |
| model offline | red ModelStatusBar, banner | composer disabled, "retry" hint |

---

## 3. Android (planned)

- **Layout:** standard chat list; `ToolCallCard` as a RecyclerView item; `ApprovalCard` rendered as a bottom-sheet dialog (the platform-native approval pattern).
- **run_code:** not present (platform ceiling). The sidebar status shows "run_code unavailable on this device — delegate to Windows or omit". No dead UI: the tool simply isn't in the registry on-device.
- **Notifications:** when the agent needs approval, post a persistent notification with Approve/Reject actions so the user can respond without keeping the app open.

---

## 4. Shared design tokens

| Token | Value | Usage |
|---|---|---|
| `color.approval` | amber | pending approval, warning states |
| `color.error` | red | failed tool calls, model offline |
| `color.tool` | neutral gray | tool call cards (never primary accent) |
| `color.user` / `color.assistant` | distinct but low-contrast | message bubbles |
| `radius.toolcard` | 8px | tool cards |
| `font.mono` | system mono | code, tool args, results |
| `spacing.toolcard` | 8px | card padding, consistent across surfaces |

Principles: tool activity is visually quieter than the conversation; approval is the loudest element; nothing auto-approves; all destructive/high-risk actions require explicit confirmation.

---

## 5. Accessibility

- All interactive components keyboard-accessible (CLI is the reference).
- Streamed text must not break screen readers — announce tool calls and approvals with status text, not just color.
- Contrast on approval card: minimum WCAG AA for code text.
- Focus management: when ApprovalCard appears, focus moves to it; `Esc` = reject.

## 6. Internationalization

- Mark all UI strings with keys at the design layer; no hardcoded text in components. Initial locale: en-US. Layout must not break with longer strings (tool descriptions, approval payloads).

## 7. Non-goals (v1)

- Voice interface (research doc lists voice as later; out of scope)
- Rich markdown rendering of tool results (plain text + mono is fine)
- Collaborative/multi-user sessions
- Agent-initiated UI (only the human starts sessions)

## 8. Implementation notes

- CLI: `cli.py` uses `core.loop.step/run`, `core.tool_registry`, and the Windows orchestrator (`windows/orchestrator.py`). ANSI only on TTY. Approval prompt maps to `resolve_approval`.
- Web: `web/` will render the exact same `AgentState` JSON; no UI logic re-implements the loop. `pending_approval` is the single source of truth for the approval card visibility.
- Android: same state, rendered natively; approval actions call `resolve_approval` equivalents.
- Session persistence: serialize `AgentState` (already pydantic). CLI stores under `~/.agent-core/sessions/`; Web uses IndexedDB/localStorage; Android uses room or file storage.