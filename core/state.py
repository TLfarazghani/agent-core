from __future__ import annotations

import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["user", "assistant", "tool", "system"]
Target = Literal["windows", "android", "webgpu"]
StepStatus = Literal["pending", "in_progress", "done", "failed", "skipped"]
PermissionMode = Literal["default", "plan", "auto", "strict"]

# Phase 8: permission modes. ``default`` asks for every approval-gated tool;
# ``plan`` allows only read-only tools (everything gated is parked for the
# human); ``auto`` auto-approves non-``run_code`` gated tools; ``strict`` asks
# even for tools that do not declare approval. ``run_code`` is never bypassable
# in any mode -- the hardcoded gate in ``tool_registry.dispatch()`` wins.
DEFAULT_PERMISSION_MODE: PermissionMode = "default"
PERMISSION_MODES: tuple[PermissionMode, ...] = ("default", "plan", "auto", "strict")


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: dict[str, Any]


class PendingApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    tool_name: str
    arguments: dict[str, Any]


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    role: Role
    content: str
    tool_call_id: Optional[str] = None
    function_calls: Optional[list[ToolCall]] = None


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    requires_approval: bool = False
    read_only: bool = False
    parameters: dict[str, Any]


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    status: StepStatus = "pending"
    result: Optional[str] = None


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    steps: list[PlanStep] = Field(default_factory=list)


class Task(BaseModel):
    """One tracked task (Phase 11).

    Distinct from ``Plan``: a plan is a single ordered procedure for the
    current request; ``tasks`` is a lightweight backlog that persists with the
    session across resumes and survives ``/new``-less reloads.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    status: StepStatus = "pending"
    result: Optional[str] = None


class TurnUsage(BaseModel):
    """One provider-generate's telemetry record (Phase 12).

    Filled from the server-reported usage providers already capture
    (llama.cpp's ``usage`` with ``stream_options.include_usage``) plus the
    generation latency. ``at`` is unix epoch seconds so sessions can be
    plotted later. Records serialize with the session and feed ``/cost``,
    ``/context``, and the web sidebar readout; ``core/telemetry.py`` folds
    them into summaries.
    """

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_s: float = 0.0
    at: float = 0.0


class AgentState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    target: Target
    model: str
    messages: list[ChatMessage] = Field(default_factory=list)
    max_turns: int = 8
    turn_count: int = 0
    pending_approval: Optional[PendingApproval] = None
    pending_calls: list[ToolCall] = Field(default_factory=list)
    retry_count: int = 0
    plan: Optional[Plan] = None
    permission_mode: PermissionMode = DEFAULT_PERMISSION_MODE
    tasks: list[Task] = Field(default_factory=list)
    active_subagents: int = 0
    tool_allowlist: Optional[list[str]] = None
    usage: list["TurnUsage"] = Field(default_factory=list)