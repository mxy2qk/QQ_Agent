from typing import Protocol

from qq_agent.messages import IncomingMessage


class ChannelAdapter(Protocol):
    def normalize(self, event: dict) -> IncomingMessage | None: ...

    async def run(self) -> None: ...
