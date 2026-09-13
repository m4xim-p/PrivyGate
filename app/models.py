"""Pydantic models for the small OpenAI-compatible API surface."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage]
    stream: bool = False

    def as_upstream_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)
