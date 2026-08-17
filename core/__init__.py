"""agent-core: transport-agnostic local LFM2.5 agent core."""

from .loop import (
    GenerateProvider,
    MaxTurnsError,
    PendingApprovalError,
    new_state,
    resolve_approval,
    run,
    step,
    user_message,
)
from .parser import (
    ParserError,
    extract_blocks,
    has_tool_call_blocks,
    parse_tool_calls,
    parse_tool_calls_strict,
)
from .state import (
    AgentState,
    ChatMessage,
    PendingApproval,
    ToolCall,
    ToolDefinition,
)
from .tool_registry import ToolRegistry

__all__ = [
    "AgentState",
    "ChatMessage",
    "GenerateProvider",
    "MaxTurnsError",
    "ParserError",
    "PendingApproval",
    "PendingApprovalError",
    "ToolCall",
    "ToolDefinition",
    "ToolRegistry",
    "extract_blocks",
    "has_tool_call_blocks",
    "new_state",
    "parse_tool_calls",
    "parse_tool_calls_strict",
    "resolve_approval",
    "run",
    "step",
    "user_message",
]