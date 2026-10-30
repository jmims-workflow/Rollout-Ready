"""Every model call lives here, so another provider could be added later.

Only Claude is implemented. Structured output uses the SDK's
messages.parse with a Pydantic schema, so judge and drafter replies
always parse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field


class ProviderError(Exception):
    """A model call failed. The message is safe to show to the person."""


@dataclass
class Generation:
    text: str
    input_tokens: int
    output_tokens: int


class JudgedCriterion(BaseModel):
    criterion_id: str
    score: int = Field(description="Whole number from 1 to 5")
    reason: str = Field(description="One sentence explaining the score")


class JudgeReply(BaseModel):
    scores: list[JudgedCriterion]


class DraftedCriterion(BaseModel):
    name: str
    five_looks_like: str
    one_looks_like: str
    deal_breaker: bool = Field(default=False, description="True if the output must never get this wrong")


class DraftReply(BaseModel):
    criteria: list[DraftedCriterion]


@dataclass
class Parsed:
    value: BaseModel
    input_tokens: int
    output_tokens: int


class Provider(Protocol):
    def generate(self, *, model: str, prompt: str, user_input: str) -> Generation: ...

    def structured(self, *, model: str, system: str, user: str, schema: type[BaseModel]) -> Parsed: ...


class ClaudeProvider:
    """Calls the Claude API. The key is read on the server only and never logged."""

    def __init__(self, api_key: str):
        import anthropic

        self._anthropic = anthropic
        # The SDK retries rate limits and brief server errors with backoff.
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=120)

    def _friendly(self, exc: Exception) -> ProviderError:
        a = self._anthropic
        if isinstance(exc, a.AuthenticationError):
            return ProviderError("Claude did not accept the API key on the server. Check ANTHROPIC_API_KEY.")
        if isinstance(exc, a.RateLimitError):
            return ProviderError("Claude is busy right now (rate limit). Wait a minute and try again; saved results are kept.")
        if isinstance(exc, a.NotFoundError):
            return ProviderError("Claude did not recognise the model name in this suite's settings.")
        if isinstance(exc, a.APIStatusError):
            return ProviderError(f"Claude returned an error ({exc.status_code}). Try again in a moment; saved results are kept.")
        if isinstance(exc, a.APIConnectionError):
            return ProviderError("Could not reach Claude. Check the server's internet connection and try again.")
        return ProviderError("Something went wrong while calling Claude. Try again in a moment.")

    @staticmethod
    def _check_stop(message) -> None:
        if getattr(message, "stop_reason", None) == "refusal":
            raise ProviderError("Claude declined to answer this one, so it could not be scored.")

    def generate(self, *, model: str, prompt: str, user_input: str) -> Generation:
        try:
            msg = self._client.messages.create(
                model=model,
                max_tokens=8000,
                system=prompt,
                messages=[{"role": "user", "content": user_input}],
            )
        except self._anthropic.AnthropicError as exc:
            raise self._friendly(exc) from None
        self._check_stop(msg)
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        return Generation(text, msg.usage.input_tokens, msg.usage.output_tokens)

    def structured(self, *, model: str, system: str, user: str, schema: type[BaseModel]) -> Parsed:
        try:
            msg = self._client.messages.parse(
                model=model,
                max_tokens=8000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
        except self._anthropic.AnthropicError as exc:
            raise self._friendly(exc) from None
        self._check_stop(msg)
        if msg.parsed_output is None:
            raise ProviderError("Claude's reply could not be read as scores. Try again.")
        return Parsed(msg.parsed_output, msg.usage.input_tokens, msg.usage.output_tokens)
