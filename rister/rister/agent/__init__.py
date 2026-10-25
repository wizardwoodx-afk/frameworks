"""Agents: models, tools, prompts, memory and the tool-calling loop."""
from __future__ import annotations

from .agent import Agent, AgentResult, coerce_structured, extract_json, json_schema_of
from .backend import (
    BoundModel,
    CallableModel,
    HTTPModel,
    Model,
    RouterModel,
    ScriptedModel,
)
from .memory import (
    FileMemoryStore,
    InMemoryStore,
    Memory,
    MemoryStore,
    SummarizingMemory,
    estimate_tokens,
)
from .prompt import Prompt, describe_tools, render_instructions
from .tool import Tool, ToolRegistry, infer_schema, parse_tool_arguments, tool

__all__ = [
    "Agent",
    "AgentResult",
    "extract_json",
    "coerce_structured",
    "json_schema_of",
    "Model",
    "BoundModel",
    "ScriptedModel",
    "CallableModel",
    "RouterModel",
    "HTTPModel",
    "Tool",
    "tool",
    "ToolRegistry",
    "infer_schema",
    "parse_tool_arguments",
    "Prompt",
    "render_instructions",
    "describe_tools",
    "Memory",
    "MemoryStore",
    "InMemoryStore",
    "FileMemoryStore",
    "SummarizingMemory",
    "estimate_tokens",
]
