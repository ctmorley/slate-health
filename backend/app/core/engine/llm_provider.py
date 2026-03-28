"""LLM provider — abstraction over AWS Bedrock Claude.

Provides a unified interface for sending prompts to Claude via AWS Bedrock
with fallback support, token tracking, and automatic PHI de-identification
before sending prompts to the LLM.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.security import deidentify_text

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when the LLM provider encounters an error."""
    pass


class LLMRateLimitError(LLMError):
    """Raised when rate-limited by the LLM provider."""
    pass


class LLMTimeoutError(LLMError):
    """Raised when the LLM call times out."""
    pass


@dataclass
class LLMResponse:
    """Structured response from an LLM call."""

    content: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    stop_reason: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenUsage:
    """Cumulative token usage tracker."""

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_calls: int = 0

    def record(self, input_tokens: int, output_tokens: int) -> None:
        """Record token usage from a single call."""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_calls += 1

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens


class LLMBackend(Protocol):
    """Protocol for LLM backend implementations (Bedrock, mock, etc.)."""

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        stop_sequences: list[str] | None = None,
    ) -> LLMResponse: ...


class BedrockBackend:
    """AWS Bedrock Claude backend.

    In production, this uses boto3 bedrock-runtime to call Claude.
    For testability, the actual boto3 call is isolated in _call_bedrock().
    """

    def __init__(
        self,
        model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0",
        region: str = "us-east-1",
        *,
        max_retries: int = 3,
        timeout: float = 60.0,
    ) -> None:
        self.model_id = model_id
        self.region = region
        self.max_retries = max_retries
        self.timeout = timeout
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy-initialize boto3 Bedrock client."""
        if self._client is None:
            try:
                import boto3

                self._client = boto3.client(
                    "bedrock-runtime",
                    region_name=self.region,
                )
            except ImportError:
                raise LLMError(
                    "boto3 is required for BedrockBackend. "
                    "Install with: pip install boto3"
                )
        return self._client

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        stop_sequences: list[str] | None = None,
    ) -> LLMResponse:
        """Send messages to Claude via Bedrock."""
        import asyncio
        import json

        client = self._get_client()

        body: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }
        if system_prompt:
            body["system"] = system_prompt
        if stop_sequences:
            body["stop_sequences"] = stop_sequences

        start_time = time.monotonic()
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                # Wrap the synchronous boto3 call in a thread executor
                # and enforce the configured timeout.
                loop = asyncio.get_running_loop()
                try:
                    response = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: client.invoke_model(
                                modelId=self.model_id,
                                body=json.dumps(body),
                                contentType="application/json",
                                accept="application/json",
                            ),
                        ),
                        timeout=self.timeout,
                    )
                except asyncio.TimeoutError:
                    raise LLMTimeoutError(
                        f"Bedrock call timed out after {self.timeout}s "
                        f"(attempt {attempt + 1}/{self.max_retries})"
                    )

                result = json.loads(response["body"].read())
                elapsed_ms = (time.monotonic() - start_time) * 1000

                content = ""
                if result.get("content"):
                    content = result["content"][0].get("text", "")

                usage = result.get("usage", {})
                return LLMResponse(
                    content=content,
                    model=self.model_id,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    latency_ms=elapsed_ms,
                    stop_reason=result.get("stop_reason", ""),
                    raw_response=result,
                )
            except LLMTimeoutError:
                last_error = LLMTimeoutError(
                    f"Bedrock call timed out after {self.timeout}s"
                )
                if attempt < self.max_retries - 1:
                    logger.warning(
                        "Bedrock timeout on attempt %d, retrying",
                        attempt + 1,
                    )
                    continue
                raise last_error
            except Exception as e:
                last_error = e
                error_str = str(e).lower()

                # Determine if this is a rate-limit error
                is_rate_limit = "throttling" in error_str or "rate" in error_str

                # ── Transient error classification ──────────────────
                # We use both isinstance checks (for boto3/botocore
                # exception types) AND string matching (for generic
                # Exception wrappers) to ensure uniform retry coverage
                # across all transient failure modes.
                is_transient = False

                # 1. Check botocore exception hierarchy (most reliable)
                try:
                    from botocore.exceptions import (
                        BotoCoreError,
                        ClientError,
                        EndpointConnectionError,
                        ConnectionClosedError,
                        ReadTimeoutError as BotoReadTimeoutError,
                        ConnectTimeoutError as BotoConnectTimeoutError,
                    )
                    if isinstance(e, (
                        EndpointConnectionError,
                        ConnectionClosedError,
                        BotoReadTimeoutError,
                        BotoConnectTimeoutError,
                    )):
                        is_transient = True
                    elif isinstance(e, ClientError):
                        # ClientError carries an error code — retry on
                        # server-side / throttling codes only.
                        code = e.response.get("Error", {}).get("Code", "")
                        _retryable_codes = {
                            "ThrottlingException", "TooManyRequestsException",
                            "ServiceUnavailableException",
                            "InternalServerException", "InternalFailure",
                            "RequestTimeout", "IDPCommunicationError",
                            "RequestLimitExceeded", "BandwidthLimitExceeded",
                            "ProviderThrottledException",
                        }
                        if code in _retryable_codes:
                            is_transient = True
                            if code in (
                                "ThrottlingException",
                                "TooManyRequestsException",
                                "RequestLimitExceeded",
                            ):
                                is_rate_limit = True
                    elif isinstance(e, BotoCoreError):
                        # Generic botocore transport failures are transient
                        is_transient = True
                except ImportError:
                    pass  # botocore not installed — fall through to string matching

                # 2. OSError / ConnectionError subclasses are always transient
                if not is_transient and isinstance(e, (ConnectionError, OSError)):
                    is_transient = True

                # 3. Fallback: string-based heuristic for wrapped exceptions
                if not is_transient:
                    _transient_indicators = (
                        "throttling", "rate", "serviceunavailable",
                        "internalserver", "internal server",
                        "service unavailable",
                        "connection", "timeout", "endpoint",
                        "temporarily", "too many requests",
                        "503", "502", "500", "429",
                        "could not connect", "network",
                    )
                    is_transient = any(
                        ind in error_str for ind in _transient_indicators
                    )

                if is_transient and attempt < self.max_retries - 1:
                    # Exponential backoff with full jitter (consistent
                    # with RetryWithBackoff used elsewhere)
                    exp_delay = 2 ** attempt
                    wait = random.uniform(0, min(exp_delay, 10.0))
                    logger.warning(
                        "Transient Bedrock error on attempt %d, retrying in %.1fs: %s",
                        attempt + 1,
                        wait,
                        e,
                    )
                    await asyncio.sleep(wait)
                    continue

                # Exhausted retries or non-transient error
                if is_rate_limit:
                    raise LLMRateLimitError(f"Rate limited after {self.max_retries} attempts: {e}")
                if is_transient:
                    raise LLMError(f"Bedrock invocation failed after {self.max_retries} transient errors: {e}")
                raise LLMError(f"Bedrock invocation failed: {e}")

        raise LLMError(f"Failed after {self.max_retries} attempts: {last_error}")


class MockLLMBackend:
    """Mock LLM backend for testing.

    Returns configurable responses without making any external API calls.
    """

    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or ["Mock LLM response"])
        self._call_index = 0
        self.call_history: list[dict[str, Any]] = []

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        stop_sequences: list[str] | None = None,
    ) -> LLMResponse:
        """Return the next configured mock response."""
        self.call_history.append(
            {
                "messages": messages,
                "system_prompt": system_prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        response_text = self._responses[
            min(self._call_index, len(self._responses) - 1)
        ]
        self._call_index += 1
        return LLMResponse(
            content=response_text,
            model="mock-model",
            input_tokens=len(str(messages)) // 4,
            output_tokens=len(response_text) // 4,
            latency_ms=1.0,
            stop_reason="end_turn",
        )


class LLMProvider:
    """High-level LLM provider with PHI safety, token tracking, and fallback.

    Wraps one or more LLM backends with:
    - Automatic PHI de-identification of prompts
    - Token usage tracking across calls
    - Fallback to secondary backend on primary failure
    """

    def __init__(
        self,
        primary: LLMBackend,
        fallback: LLMBackend | None = None,
        *,
        phi_safe: bool = True,
        additional_names: list[str] | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._phi_safe = phi_safe
        self._additional_names = additional_names
        self.token_usage = TokenUsage()

    def _deidentify_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Strip PHI from message content before sending to LLM."""
        if not self._phi_safe:
            return messages

        safe_messages = []
        for msg in messages:
            safe_msg = dict(msg)
            if isinstance(safe_msg.get("content"), str):
                safe_msg["content"] = deidentify_text(
                    safe_msg["content"],
                    additional_names=self._additional_names,
                )
            safe_messages.append(safe_msg)
        return safe_messages

    def _deidentify_system(self, system_prompt: str | None) -> str | None:
        """Strip PHI from system prompt."""
        if not self._phi_safe or not system_prompt:
            return system_prompt
        return deidentify_text(
            system_prompt,
            additional_names=self._additional_names,
        )

    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        stop_sequences: list[str] | None = None,
    ) -> LLMResponse:
        """Send a prompt to the LLM with PHI safety and fallback.

        Args:
            messages: Conversation messages (role/content dicts).
            system_prompt: Optional system prompt.
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
            stop_sequences: Sequences that stop generation.

        Returns:
            LLMResponse with content and usage stats.

        Raises:
            LLMError: If both primary and fallback fail.
        """
        safe_messages = self._deidentify_messages(messages)
        safe_system = self._deidentify_system(system_prompt)

        try:
            response = await self._primary.invoke(
                safe_messages,
                system_prompt=safe_system,
                max_tokens=max_tokens,
                temperature=temperature,
                stop_sequences=stop_sequences,
            )
        except LLMError:
            if self._fallback is None:
                raise
            logger.warning("Primary LLM failed, attempting fallback")
            response = await self._fallback.invoke(
                safe_messages,
                system_prompt=safe_system,
                max_tokens=max_tokens,
                temperature=temperature,
                stop_sequences=stop_sequences,
            )

        self.token_usage.record(response.input_tokens, response.output_tokens)
        logger.info(
            "LLM call: model=%s input_tokens=%d output_tokens=%d latency=%.1fms",
            response.model,
            response.input_tokens,
            response.output_tokens,
            response.latency_ms,
        )
        return response
