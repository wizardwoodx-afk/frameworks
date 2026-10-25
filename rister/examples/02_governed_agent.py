"""02 — A governed agent: tools, policy, approval, audit, memory.

No network and no API key: ScriptedModel stands in for the model, so the example is
deterministic. Swap in HTTPModel(model=..., base_url=...) for a real one.

Run:  python examples/02_governed_agent.py
"""
from __future__ import annotations

import rister as rs

NOTES = {"governance": "Policies must be enforced in code, not in prompts."}


@rs.tool
def search_notes(query: str) -> str:
    """Search the internal notes store."""
    for key, value in NOTES.items():
        if key in query.lower():
            return value
    return "no notes found"


@rs.tool
def delete_notes(scope: str) -> str:
    """Delete notes. Destructive: policy denies this."""
    return f"deleted {scope}"


@rs.tool(requires_approval=True, tags=["outbound"])
def publish_note(text: str) -> str:
    """Publish a note to the shared wiki. Requires sign-off."""
    return f"published: {text}"


policy = rs.Policy.from_dict({
    "name": "demo",
    "default": "deny",
    "rules": [
        {"name": "read", "action": ["search_notes"], "effect": "allow"},
        {"name": "delete", "action": ["delete_*"], "effect": "deny",
         "reason": "destructive operations are blocked"},
        {"name": "publish", "action": "publish_note", "effect": "allow"},
    ],
})

audit = rs.AuditLog(key="demo-signing-key", name="demo")
guard = rs.Guard(policy=policy, audit=audit, approver=rs.AutoApprover())

agent = rs.Agent(
    "analyst",
    model=rs.ScriptedModel([
        # turn 1: look something up
        [rs.ToolCall("search_notes", {"query": "governance"}, id="c1")],
        # turn 2: try to destroy data, then publish (which needs sign-off)
        [rs.ToolCall("delete_notes", {"scope": "all"}, id="c2"),
         rs.ToolCall("publish_note", {"text": "audit summary"}, id="c3")],
        # turn 3: report what happened
        "Governance is enforced in code. I published the summary; the delete was refused.",
    ], name="scripted-analyst"),
    instructions="You are a careful analyst. Only use approved tools.",
    tools=[search_notes, delete_notes, publish_note],
    memory=rs.Memory(rs.InMemoryStore(), window=12),
    guard=guard,
    max_turns=4,
)

result = agent.run("Summarise the governance notes", {"thread_id": "t1"})

print("1. what the model said")
print(f"   {result.text}")
print(f"   allowed tool calls: {[c['tool'] for c in result.tool_calls]}")

print("2. what the governance layer refused")
for denial in result.denied:
    print(f"   {denial['tool']}: {denial['reason']}")

print("3. the audit trail")
verification = audit.verify()
print(f"   {len(audit)} entries, chain valid={verification.ok}")
for entry in audit:
    print(f"   {entry.action:<14} {entry.effect:<8} {entry.reason}")

print("4. tampering is detectable")
entries = [entry.to_dict() for entry in audit]
print(f"   entry 1 was: {entries[0]['action']} -> {entries[0]['effect']}")
entries[0]["effect"] = "deny"          # rewrite history: turn a denial into a pass
forged = rs.AuditLog(key="demo-signing-key")
forged.extend([rs.AuditEntry.from_dict(item) for item in entries])
result_of_check = forged.verify()
print(f"   edited log verifies: {result_of_check.ok} "
      f"(broken at seq {result_of_check.broken_at})")

print("5. memory persists across calls on the same thread")
agent.memory.append("t1", rs.Message.user("and what about retention?"))
print(f"   messages on thread t1: {len(agent.memory.history('t1'))}")
