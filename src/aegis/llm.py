"""LLM client wrapper.

All Claude calls go through AWS Bedrock — never the direct Anthropic API.
The `BedrockClient` is the production implementation. The `LLMClient`
Protocol exists so tests can inject a fake without touching boto3 / AWS.

Boot guard: importing this module triggers `get_settings()` so the
data-residency check runs before any LLM call can be issued. Independently,
`BedrockClient.__init__` re-checks that the model id starts with "us." —
defense in depth in case settings get mutated.

Resilience
----------
Bedrock occasionally returns transient HTTP errors (429 throttling, 5xx
internal, gateway timeouts) and the underlying httpx transport can hiccup
on flaky networks. Both LLM entry points are wrapped with `tenacity` to
retry up to 3 times with exponential backoff. We deliberately do NOT retry
on `ValidationError` or `DataResidencyError` — those are deterministic
configuration / schema bugs and retrying makes them worse, not better.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import anthropic
import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from aegis.config import DataResidencyError, get_settings

# HTTP status codes that indicate a transient Bedrock failure worth retrying.
# 429 = rate limit / throttling, 500/502/503/504 = upstream / gateway issues.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _is_retryable_bedrock_error(exc: BaseException) -> bool:
    """Return True iff the exception is a transient Bedrock / network failure.

    Retried:
      - `anthropic.APIStatusError` with status_code in _RETRYABLE_STATUS
      - `httpx.TransportError` (connection reset, dns failure, timeout, …)

    Not retried (deterministic / config bugs):
      - 4xx other than 429 (BadRequest, Auth, Permission, NotFound, …)
      - `DataResidencyError`, `ValueError` (malformed JSON), Pydantic errors
    """
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code in _RETRYABLE_STATUS
    if isinstance(exc, httpx.TransportError):
        return True
    return False


class LLMClient(Protocol):
    """Minimal interface the parser needs from the LLM.

    Two methods, mapping to the two-pass parser flow.
    Tests provide stubs implementing this Protocol.
    """

    def extract_raw_json(
        self, pdf_bytes: bytes, prompt: str
    ) -> tuple[dict[str, Any], bool]:
        """Run the extraction pass: PDF in, raw JSON + truncation flag out.

        The bool is True iff Bedrock cut output at `max_tokens`. The
        validator surfaces this as `extraction_truncated_retry_required`
        instead of letting truncation get misdiagnosed as a math failure.
        """

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
        """Run extraction over rasterised page images (OCR fallback path).

        Each entry of `page_images_png` is a PNG-encoded image of one PDF
        page, in 1-indexed page order. Returned tuple shape matches
        `extract_raw_json` so the rest of the parser is identical.
        """

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        """Run a classification batch: instructions+payload in, classification JSON out."""


class BedrockClient:
    """Production LLM client backed by AWS Bedrock.

    Uses `AnthropicBedrock` (boto3 credential chain). Reads settings only;
    does not accept overrides — environment is the single source of truth.
    """

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.bedrock_model_id.startswith("us."):
            # Should be caught by the field_validator in config, but defense in depth.
            raise DataResidencyError(
                f"BedrockClient refusing non-US model: {settings.bedrock_model_id}"
            )
        self._model = settings.bedrock_model_id
        self._client = anthropic.AnthropicBedrock(aws_region=settings.aws_region)

    @retry(
        retry=retry_if_exception(_is_retryable_bedrock_error),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def extract_raw_json(
        self, pdf_bytes: bytes, prompt: str
    ) -> tuple[dict[str, Any], bool]:
        """Send a PDF document block + extraction prompt; parse the first JSON object out.

        Uses streaming because real bank statements can produce >16K tokens of
        JSON output, which exceeds Bedrock's non-streaming 10-minute SLA window.

        Returns a (parsed_json, truncated) tuple. `truncated` is True when
        Bedrock stopped at `max_tokens` — downstream validation surfaces
        this as a dedicated failure code so a cut-off response doesn't get
        misdiagnosed as a math reconciliation bug.
        """
        import base64

        with self._client.messages.stream(
            model=self._model,
            max_tokens=64000,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": base64.b64encode(pdf_bytes).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        ) as stream:
            response = stream.get_final_message()
        truncated = getattr(response, "stop_reason", None) == "max_tokens"
        return _first_json_object(_text_blocks(response)), truncated

    @retry(
        retry=retry_if_exception(_is_retryable_bedrock_error),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
        """Vision-pass extraction: page images + extraction prompt -> JSON.

        Same retry / streaming semantics as `extract_raw_json`. Image
        blocks are sent first (in page order so the model can refer to
        them as page 1, page 2, ...) followed by the text prompt.
        """
        import base64

        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(png).decode("ascii"),
                },
            }
            for png in page_images_png
        ]
        content.append({"type": "text", "text": prompt})

        with self._client.messages.stream(
            model=self._model,
            max_tokens=64000,
            messages=[{"role": "user", "content": content}],  # type: ignore[typeddict-item]
        ) as stream:
            response = stream.get_final_message()
        truncated = getattr(response, "stop_reason", None) == "max_tokens"
        return _first_json_object(_text_blocks(response)), truncated

    @retry(
        retry=retry_if_exception(_is_retryable_bedrock_error),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        return _first_json_object(_text_blocks(response))


def _text_blocks(response: object) -> str:
    """Concatenate all text blocks from an Anthropic Messages response."""
    pieces: list[str] = []
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "text":
            pieces.append(block.text)
    return "\n".join(pieces)


def _first_json_object(text: str) -> dict[str, Any]:
    """Extract the first {...} JSON object in a text response.

    If the response parses to a top-level JSON array, raise a clear error
    pointing at `parser/prompts.py` so operators know the prompt — not the
    parser — is the place to fix the schema mismatch.
    """
    start = text.find("{")
    end = text.rfind("}")
    bracket_start = text.find("[")
    # If the response is a bare array (no object wrapper), give a clearer
    # error than "no JSON object". This lands in `aegis.parser.extract`
    # logs as `LLM returned malformed JSON: ...`, and the operator's first
    # action should be to inspect / amend the prompt.
    if (start == -1 or end == -1 or end < start) and bracket_start != -1:
        raise ValueError(
            "LLM returned a top-level JSON array; expected an object. "
            "Fix the prompt in `aegis/parser/prompts.py` to require an "
            "object wrapper (e.g. {\"classifications\": [...]})."
        )
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object in LLM response: {text[:200]!r}")
    snippet = text[start : end + 1]
    try:
        parsed = json.loads(snippet)
    except json.JSONDecodeError as exc:
        raise ValueError(f"could not parse JSON: {exc}; raw={snippet[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            "top-level JSON is not an object: got "
            f"{type(parsed).__name__}. Fix the prompt in "
            "`aegis/parser/prompts.py` to require an object wrapper."
        )
    return parsed


__all__ = ["BedrockClient", "LLMClient"]
