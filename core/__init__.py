"""agent-core: transport-agnostic local LFM2.5 agent core."""

from .context import estimate_message_tokens, estimate_tokens, trim_to_budget
from .loop import (
    RETRY_ONCE_LIMIT,
    GenerateProvider,
    MaxTurnsError,
    PendingApprovalError,
    finalize_turn,
    new_state,
    resolve_approval,
    run,
    should_stop_after_retries,
    step,
    user_message,
)
from .memory import (
    MEMORY_DIR,
    KINDS,
    list_memories,
    load_memory,
    memory_path,
    recall_bounded,
    recall_memories,
    save_memory,
)
from .meta import AGENT_NAME, agent_bio, current_time, inspect_self
from .parser import (
    ParserError,
    extract_blocks,
    has_tool_call_blocks,
    parse_tool_calls,
    parse_tool_calls_strict,
)
from .planner import make_plan as planner_make_plan
from .planner import update_plan as planner_update_plan
from .reflection import lesson_from_state, maybe_emit_lesson
from .state import (
    AgentState,
    ChatMessage,
    PendingApproval,
    Plan,
    PlanStep,
    ToolCall,
    ToolDefinition,
)
from .tool_registry import ToolRegistry, takes_state

__all__ = [
    "AGENT_NAME",
    "AgentState",
    "ChatMessage",
    "GenerateProvider",
    "KINDS",
    "MaxTurnsError",
    "MEMORY_DIR",
    "ParserError",
    "PendingApproval",
    "PendingApprovalError",
    "Plan",
    "PlanStep",
    "RETRY_ONCE_LIMIT",
    "ToolCall",
    "ToolDefinition",
    "ToolRegistry",
    "agent_bio",
    "current_time",
    "estimate_message_tokens",
    "estimate_tokens",
    "extract_blocks",
    "finalize_turn",
    "has_tool_call_blocks",
    "inspect_self",
    "lesson_from_state",
    "list_memories",
    "load_memory",
    "maybe_emit_lesson",
    "memory_path",
    "new_state",
    "parse_tool_calls",
    "parse_tool_calls_strict",
    "planner_make_plan",
    "planner_update_plan",
    "recall_bounded",
    "recall_memories",
    "resolve_approval",
    "run",
    "save_memory",
    "should_stop_after_retries",
    "step",
    "takes_state",
    "trim_to_budget",
    "user_message",
]