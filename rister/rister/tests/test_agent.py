"""Agents, tools, memory, prompts and structured output."""
from __future__ import annotations

import tempfile

from rister.agent import (
    Agent,
    FileMemoryStore,
    InMemoryStore,
    Memory,
    Prompt,
    ScriptedModel,
    SummarizingMemory,
    ToolRegistry,
    describe_tools,
    tool,
)
from rister.agent.agent import coerce_structured, extract_json
from rister.agent.backend import CallableModel, HTTPModel
from rister.core.errors import ToolError
from rister.core.types import Message, ToolCall


@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@tool
def multiply(a: int, b: int) -> int:
    """Multiply two numbers."""
    return a * b


@tool(requires_approval=True)
def publish(url: str) -> str:
    """Publish a document."""
    return f"published {url}"


# -- tools -----------------------------------------------------------------
def test_tool_schema_inference():
    spec = add.spec()
    assert spec["parameters"]["properties"]["a"] == {"type": "integer"}
    assert spec["parameters"]["required"] == ["a", "b"]
    assert spec["description"] == "Add two numbers."
    assert add.as_function_spec()["type"] == "function"


def test_tool_argument_validation():
    assert add.validate({"a": 1, "b": 2}) == []
    problems = add.validate({"c": 1})
    assert any("unknown argument" in p for p in problems)
    assert any("missing required" in p for p in problems)


def test_tool_registry_duplicates_and_unknown():
    registry = ToolRegistry([add])
    try:
        registry.add(add)
        raise AssertionError("expected ToolError")
    except ToolError:
        pass
    try:
        registry.get("missing")
        raise AssertionError("expected ToolError")
    except ToolError:
        pass


def test_tool_registry_guard_denial():
    class Denier:
        def check(self, action, **kwargs):
            from rister.govern import deny

            return deny(reason="nope") if action == "add" else None

    registry = ToolRegistry([add])
    from rister.core.errors import PolicyDenied

    try:
        registry.call("add", {"a": 1, "b": 2}, guard=Denier())
        raise AssertionError("expected PolicyDenied")
    except PolicyDenied:
        pass


# -- agent loop ------------------------------------------------------------
def test_agent_runs_tools_to_completion():
    model = ScriptedModel(
        [
            {"tool_calls": [ToolCall("add", {"a": 2, "b": 3})]},
            {"tool_calls": [ToolCall("multiply", {"a": 5, "b": 4})]},
            "The answer is 20.",
        ]
    )
    agent = Agent("calc", model=model, instructions="be brief", tools=[add, multiply])
    result = agent.run("(2+3)*4?")
    assert result.ok and result.text == "The answer is 20."
    assert [call["result"] for call in result.tool_calls] == [5, 20]
    assert result.usage.total_tokens == 0  # scripted models report no usage


def test_agent_recovers_from_unknown_tool():
    model = ScriptedModel([{"tool_calls": [ToolCall("ghost", {})]}, "recovered"])
    result = Agent("x", model=model, tools=[add]).run("go")
    assert result.text == "recovered" and len(result.denied) == 1


def test_agent_max_turns():
    model = ScriptedModel([{"tool_calls": [ToolCall("add", {"a": 1, "b": 1})]}])
    result = Agent("loop", model=model, tools=[add], max_turns=3).run("loop")
    assert result.status == "max_turns" and result.turns == 3


def test_approval_tool_without_guard_is_refused():
    model = ScriptedModel([{"tool_calls": [ToolCall("publish", {"url": "x"})]}, "ok"])
    result = Agent("p", model=model, tools=[publish]).run("go")
    assert result.denied and "APPROVAL_REQUIRED" in str(result.denied[0]["result"])


def test_stream_excludes_internal_result():
    agent = Agent("a", model=ScriptedModel(["done"]))
    events = [event["event"] for event in agent.stream("hi")]
    assert events[0] == "run_start" and events[-1] == "run_end"
    assert "__result__" not in events


def test_memory_roundtrip():
    memory = Memory(InMemoryStore(), window=5)
    agent = Agent("chat", model=ScriptedModel(["one", "two"]), memory=memory)
    agent.run("first", {"thread_id": "t"})
    agent.run("second", {"thread_id": "t"})
    roles = [m.role.value for m in memory.history("t")]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_file_memory_store_durable():
    with tempfile.TemporaryDirectory() as directory:
        store = FileMemoryStore(directory)
        memory = Memory(store)
        memory.append("t1", Message.user("hello"))
        assert Memory(FileMemoryStore(directory)).history("t1")[0].text == "hello"


def test_summarizing_memory_compresses():
    memory = SummarizingMemory(InMemoryStore(), window=2, summarize_after=4)
    for index in range(6):
        memory.append("t", Message.user(f"message {index}"))
    history = memory.history("t")
    assert any("Summary of earlier conversation" in m.text for m in history)
    assert len(history) <= 4


def test_structured_output():
    class Person:
        name: str
        age: int

    model = ScriptedModel(['```json\n{"name": "Ada", "age": "36"}\n```'])
    result = Agent("extract", model=model, output_schema=Person).run("who?")
    assert result.data == {"name": "Ada", "age": 36}
    assert result.ok


def test_structured_output_missing_key():
    class Person:
        name: str
        age: int

    model = ScriptedModel(['{"name": "Ada"}'])
    result = Agent("extract", model=model, output_schema=Person).run("who?")
    assert result.status == "incomplete" and "missing required" in result.text


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('text ```json\n{"a": 2}\n``` more') == {"a": 2}
    assert extract_json('prefix [1, 2]') == [1, 2]


def test_coerce_structured_types():
    spec = {"properties": {"n": {"type": "integer"}}, "required": ["n"]}
    assert coerce_structured(spec, {"n": "7"}) == {"n": 7}


def test_text_tool_calling():
    model = ScriptedModel(
        ['{"tool": "add", "arguments": {"a": 7, "b": 8}}', '"15"']
    )
    result = Agent("weak", model=model, tools=[add], text_tools=True).run("7+8?")
    assert result.tool_calls and result.tool_calls[0]["result"] == 15


def test_agent_as_tool_and_node():
    sub = Agent("researcher", model=ScriptedModel(["research done"]))
    parent_model = ScriptedModel(
        [{"tool_calls": [ToolCall("ask_researcher", {"task": "find"})]}, "done"]
    )
    parent = Agent("lead", model=parent_model, tools=[sub.as_tool()])
    assert parent.run("go").tool_calls[0]["result"] == "research done"

    from rister.graph import END, START, Graph
    from rister.state import State

    class G(State):
        task: str = ""
        output: str = ""
        usage_total: int = 0

    graph = Graph(G, "g")
    graph.add_node("w", Agent("w", model=ScriptedModel(["node out"])).as_node(input_key="task"))
    graph.add_edge(START, "w")
    graph.add_edge("w", END)
    assert graph.compile().invoke({"task": "x"}).values["output"] == "node out"


# -- prompts & backends ----------------------------------------------------
def test_prompt_templating_and_instructions():
    prompt = Prompt("Write about {topic} for {audience}", defaults={"audience": "engineers"})
    assert prompt.render(topic="graphs") == "Write about graphs for engineers"
    assert prompt.render(topic="x", audience="kids").endswith("kids")
    assert "{unknown}" in Prompt("{unknown}").render()


def test_describe_tools_mentions_names():
    text = describe_tools([add.spec()])
    assert "add" in text and "integer" in text


def test_callable_model_and_bound_options():
    seen = {}

    def fn(messages, tools, **options):
        seen.update(options)
        return "ok"

    model = CallableModel(fn).bind(temperature=0.1)
    assert model.complete([Message.user("hi")]).text == "ok"
    assert seen["temperature"] == 0.1


def test_http_payload_shape():
    model = HTTPModel(model="demo", base_url="https://example.invalid/v1", api_key="k")
    payload = model.payload([Message.user("hi")], tools=[add.as_function_spec()], temperature=0.2)
    assert payload["model"] == "demo" and payload["temperature"] == 0.2
    assert payload["tools"][0]["function"]["name"] == "add"
    assert payload["messages"][0] == {"role": "user", "content": "hi"}


def test_scripted_model_accepts_a_plain_list_of_tool_calls():
    """A natural script entry: "call these tools now"."""
    from rister.agent import tool

    @tool
    def lookup(query: str) -> str:
        """Look something up."""
        return f"found {query}"

    agent = Agent(
        "a",
        model=ScriptedModel([
            [ToolCall("lookup", {"query": "x"}), ToolCall("lookup", {"query": "y"})],
            "finished",
        ]),
        tools=[lookup],
    )
    result = agent.run("go")
    assert result.text == "finished"
    assert [call["tool"] for call in result.tool_calls] == ["lookup", "lookup"]
    assert result.tool_calls[1]["result"] == "found y"
