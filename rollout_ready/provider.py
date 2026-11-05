"""Every model call lives here, so another provider could be added later.

Only Claude is implemented. Structured output uses the SDK's
messages.parse with a Pydantic schema, so judge and drafter replies
always parse.

Retries are ours, not the SDK's (max_retries=0), so the rule is easy to see
and to test: call_with_retries retries a temporary failure (rate limit,
overloaded, server error, timeout, dropped connection) at most twice, with
increasing waits, and honors any retry-after signal. A reply cut off at the
output limit is not an error: it comes back with stop_reason "max_tokens",
and the caller decides, because the same request would be cut off again.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from pydantic import BaseModel, Field

MAX_OUTPUT_TOKENS = 8000  # part of the generation settings, so it is part of the answer key
MAX_RETRIES = 2
BASE_WAIT_SECONDS = 2.0
CUT_OFF = "max_tokens"  # the stop reason for a reply cut off at the output limit
CUT_OFF_MESSAGE = "This answer was cut off at the length limit."


class ProviderError(Exception):
    """A model call failed. The message is safe to show to the person.

    temporary: worth retrying automatically (rate limit, overloaded, server error,
    timeout, dropped connection). retry_after: seconds the service asked us to wait.
    kind: a short machine-readable reason. input_tokens and output_tokens count
    tokens spent on failed attempts, so Behind the scenes can include them.
    """

    def __init__(self, message: str, *, temporary: bool = False, retry_after: float | None = None,
                 kind: str = "other", input_tokens: int = 0, output_tokens: int = 0):
        super().__init__(message)
        self.temporary = temporary
        self.retry_after = retry_after
        self.kind = kind
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


@dataclass
class Generation:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str = "end_turn"

    @property
    def cut_off(self) -> bool:
        return self.stop_reason == CUT_OFF


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
    value: BaseModel | None  # None when the reply was cut off
    input_tokens: int
    output_tokens: int
    stop_reason: str = "end_turn"
    attempts: int = 1
    failed_input_tokens: int = 0  # already included in input_tokens
    failed_output_tokens: int = 0  # already included in output_tokens

    @property
    def cut_off(self) -> bool:
        return self.stop_reason == CUT_OFF


@dataclass
class Attempted:
    """What call_with_retries returns: the value, and what the failed attempts cost."""
    value: Any
    attempts: int = 1
    failed_input_tokens: int = 0
    failed_output_tokens: int = 0


def call_with_retries(fn: Callable[[], Any], *, sleep: Callable[[float], None] = time.sleep,
                      max_retries: int = MAX_RETRIES, base_wait: float = BASE_WAIT_SECONDS) -> Attempted:
    """Call fn(), retrying a temporary ProviderError at most max_retries times.

    Waits base_wait, then twice that, unless the service sent a retry-after time.
    Anything else (cut-off replies, invalid requests, key or billing problems,
    anything the person can fix) is never retried here. When retries run out,
    the last error is raised with the tokens of every failed attempt on it.
    """
    failed_in = failed_out = 0
    attempt = 0
    while True:
        attempt += 1
        try:
            value = fn()
        except ProviderError as exc:
            failed_in += exc.input_tokens
            failed_out += exc.output_tokens
            if not exc.temporary or attempt > max_retries:
                exc.input_tokens, exc.output_tokens = failed_in, failed_out
                raise
            wait = exc.retry_after if exc.retry_after is not None else base_wait * (2 ** (attempt - 1))
            sleep(max(0.0, wait))
            continue
        return Attempted(value, attempt, failed_in, failed_out)


class Provider(Protocol):
    def generate(self, *, model: str, prompt: str, user_input: str) -> Generation: ...

    def structured(self, *, model: str, system: str, user: str, schema: type[BaseModel]) -> Parsed: ...


class ClaudeProvider:
    """Calls the Claude API. The key is read on the server only and never logged."""

    def __init__(self, api_key: str):
        import anthropic

        self._anthropic = anthropic
        # Retries are done by call_with_retries, so the SDK makes one attempt per call.
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=120)

    @staticmethod
    def _retry_after(exc: Exception) -> float | None:
        """Seconds from a retry-after header, if the service sent one."""
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None) or {}
        raw = headers.get("retry-after") if hasattr(headers, "get") else None
        try:
            return max(0.0, float(raw)) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def _friendly(self, exc: Exception) -> ProviderError:
        a = self._anthropic
        if isinstance(exc, a.AuthenticationError):
            return ProviderError("Claude did not accept the API key on the server. Check ANTHROPIC_API_KEY.", kind="auth")
        if isinstance(exc, a.PermissionDeniedError):
            return ProviderError("The API key on the server isn't allowed to do this. Check its permissions.", kind="auth")
        if isinstance(exc, a.RateLimitError):
            return ProviderError("Claude is busy right now (rate limit). Wait a minute and try again; saved results are kept.",
                                 temporary=True, retry_after=self._retry_after(exc), kind="rate_limit")
        if isinstance(exc, a.NotFoundError):
            return ProviderError("Claude did not recognise the model name in this suite's settings.", kind="not_found")
        if isinstance(exc, a.BadRequestError):
            return ProviderError("Claude could not accept this request as written. Check the instructions and examples, then try again.",
                                 kind="invalid")
        if isinstance(exc, a.APIStatusError):
            status = exc.status_code
            if status == 402:
                return ProviderError("The Claude account on the server is out of credit. Add credit, then try again.", kind="billing")
            if status == 529:
                return ProviderError("Claude is overloaded right now. Try again in a moment; saved results are kept.",
                                     temporary=True, retry_after=self._retry_after(exc), kind="overloaded")
            if status >= 500:
                return ProviderError(f"Claude returned an error ({status}). Try again in a moment; saved results are kept.",
                                     temporary=True, retry_after=self._retry_after(exc), kind="server")
            return ProviderError(f"Claude returned an error ({status}). Check the settings and try again.", kind="invalid")
        if isinstance(exc, a.APITimeoutError):
            return ProviderError("Claude took too long to answer. Try again in a moment; saved results are kept.",
                                 temporary=True, kind="timeout")
        if isinstance(exc, a.APIConnectionError):
            return ProviderError("Could not reach Claude. Check the server's internet connection and try again.",
                                 temporary=True, kind="connection")
        return ProviderError("Something went wrong while calling Claude. Try again in a moment.")

    @staticmethod
    def _check_stop(message) -> None:
        if getattr(message, "stop_reason", None) == "refusal":
            usage = getattr(message, "usage", None)
            raise ProviderError("Claude declined to answer this one, so it could not be scored.", kind="refusal",
                                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                                output_tokens=getattr(usage, "output_tokens", 0) or 0)

    def generate(self, *, model: str, prompt: str, user_input: str) -> Generation:
        try:
            msg = self._client.messages.create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=prompt,
                messages=[{"role": "user", "content": user_input}],
            )
        except self._anthropic.AnthropicError as exc:
            raise self._friendly(exc) from None
        self._check_stop(msg)
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        return Generation(text, msg.usage.input_tokens, msg.usage.output_tokens, msg.stop_reason or "end_turn")

    def structured(self, *, model: str, system: str, user: str, schema: type[BaseModel]) -> Parsed:
        try:
            msg = self._client.messages.parse(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
        except self._anthropic.AnthropicError as exc:
            raise self._friendly(exc) from None
        self._check_stop(msg)
        if msg.stop_reason == CUT_OFF:
            return Parsed(None, msg.usage.input_tokens, msg.usage.output_tokens, CUT_OFF)
        if msg.parsed_output is None:
            raise ProviderError("Claude's reply could not be read as scores. Try again.", kind="unreadable",
                                input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens)
        return Parsed(msg.parsed_output, msg.usage.input_tokens, msg.usage.output_tokens, msg.stop_reason or "end_turn")
