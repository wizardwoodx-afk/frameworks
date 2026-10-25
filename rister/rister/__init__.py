"""RISTER — a unified, native toolkit for governed AI agents.

One coherent stack instead of six overlapping ones:

* **orchestration**  — typed state, graphs, supersteps, interrupts, snapshots
* **agents**         — model backends, tools, memory, structured output
* **governance**     — policy, identity, approvals, audit chain, sandbox, rings
* **security**       — static rules, repository scanner, SARIF, policy drafting
* **observability**  — spans, exporters, summaries
* **evaluation**     — cases, deterministic scorers, suites, reports
* **serving**        — a dependency-free HTTP surface

Quick start::

    import rister as rs

    class Draft(rs.State):
        topic: str = ""
        notes: list = rs.Append()

    graph = rs.Graph(Draft, "draft")
    graph.add_node("write", lambda s: {"notes": [f"about {s.topic}"]})
    graph.add_edge(rs.START, "write")
    graph.add_edge("write", rs.END)
    print(graph.compile().invoke({"topic": "governance"}).values)
"""
from __future__ import annotations

from . import agent, core, evals, govern, graph, observe, security
from .agent import (
    Agent,
    AgentResult,
    CallableModel,
    HTTPModel,
    InMemoryStore,
    Memory,
    Model,
    Prompt,
    ScriptedModel,
    Tool,
    ToolRegistry,
    tool,
)
from .core import (
    ApprovalRequired,
    Completion,
    Config,
    Interrupt,
    Message,
    ModelError,
    PolicyDenied,
    RisterError,
    Role,
    SandboxError,
    StateError,
    Suspend,
    ToolCall,
    ToolError,
    Usage,
)
from .evals import Case, Contains, Exact, JsonKeys, NumericClose, Regex, Suite, evaluate
from .govern import (
    ALLOW,
    APPROVE,
    DENY,
    AuditEntry,
    AuditLog,
    AutoApprover,
    Decision,
    Guard,
    Identity,
    Policy,
    PolicySet,
    PromptDefense,
    QueueApprover,
    Ring,
    Rule,
    Sandbox,
    Signer,
    SupplyChainGuard,
    evaluate_prompt,
)
from .graph import (
    END,
    START,
    Command,
    CompiledGraph,
    FileSnapshotStore,
    Graph,
    MemorySnapshotStore,
    RunResult,
    Snapshot,
    SnapshotStore,
    goto,
)
from .observe import ConsoleExporter, JSONLExporter, Span, Tracer, get_tracer, set_tracer
from .security import (
    ScanReport,
    Scanner,
    Suppression,
    draft_security_policy,
    get_rules,
    scan,
)
from .serve import App, create_app
from .state import Append, Last, Max, Merge, Min, Reducer, State, Sum, Unique

__version__ = "1.0.0"
__all__ = [
    "__version__",
    # subpackages
    "core", "agent", "graph", "govern", "security", "observe", "evals",
    # state
    "State", "Reducer", "Last", "Append", "Unique", "Merge", "Sum", "Max", "Min",
    # graph
    "Graph", "CompiledGraph", "Command", "goto", "START", "END", "RunResult",
    "Snapshot", "SnapshotStore", "MemorySnapshotStore", "FileSnapshotStore",
    # agent
    "Agent", "AgentResult", "Model", "ScriptedModel", "CallableModel", "HTTPModel",
    "Tool", "tool", "ToolRegistry", "Memory", "InMemoryStore", "Prompt",
    # core types
    "Message", "Role", "ToolCall", "Usage", "Completion", "Config",
    "RisterError", "StateError", "ToolError", "ModelError", "Interrupt", "Suspend",
    "PolicyDenied", "ApprovalRequired", "SandboxError",
    # governance
    "Policy", "PolicySet", "Rule", "Decision", "Guard", "AuditEntry", "AuditLog", "Identity",
    "Signer", "Ring", "Sandbox", "QueueApprover", "AutoApprover", "PromptDefense",
    "evaluate_prompt", "SupplyChainGuard", "ALLOW", "DENY", "APPROVE",
    # security
    "Scanner", "Suppression", "ScanReport", "scan", "get_rules", "draft_security_policy",
    # observe
    "Tracer", "Span", "JSONLExporter", "ConsoleExporter", "get_tracer", "set_tracer",
    # evals
    "Suite", "Case", "evaluate", "Exact", "Contains", "Regex", "JsonKeys", "NumericClose",
    # serve
    "App", "create_app",
]
