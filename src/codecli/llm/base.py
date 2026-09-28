from typing import Protocol, Any, AsyncIterator

from codecli.llm.pricing import CostBreakdown
from codecli.types import Message, Usage


class LlmClient(Protocol):
    model_name: str
    provider_name: str
    max_context_window: int
    price_profile: None

    def chat(
            self,
            message: list[Message],
            tools: list[dict[str, Any]],
            *,
            system_prompt: str
    ) -> AsyncIterator[dict[str, Any]]: ...

    def calculate_cost(
            self,
            usage: Usage | dict[str, Any],
            *,
            currency: str = "usd",
    ) -> CostBreakdown: ...
