"""Synthetic tools used only by tests, never registered in the running Agent."""

from qq_agent.tools import Tool, ToolArguments, ToolRegistry


class EchoArguments(ToolArguments):
    value: str = "ok"


def sample_registry():
    async def echo(value="ok"):
        if value == "invalid":
            raise ValueError("synthetic failure")
        return {"value": value}

    registry = ToolRegistry()
    registry.register(Tool("test_echo", "Synthetic test echo", EchoArguments, echo))
    return registry
