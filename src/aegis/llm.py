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

    def extract_raw_json(self, pdf_bytes: bytes, prompt: str) -> tuple[dict[str, Any], bool]:
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


# ``invoke_tool_json`` is INTENTIONALLY not on the ``LLMClient`` Protocol.
# Adding it would force every test stub (~80 of them) to implement an
# unrelated method. The narrator carries its own narrow Protocol
# (``aegis.scoring_v2.narrator._NarratorClient``) and the route handler
# at ``aegis.web.routers.merchants.merchant_detail`` runtime-narrows
# ``get_llm()``'s ``LLMClient`` return to that Protocol with ``cast``;
# the production ``BedrockClient`` implements both Protocols so the
# narrowing is sound.


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
    def extract_raw_json(self, pdf_bytes: bytes, prompt: str) -> tuple[dict[str, Any], bool]:
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

    def generate_text(self, prompt: str, *, max_tokens: int = 512) -> str:
        """Send a plain text prompt and return the model's response text.

        Non-streaming. No JSON parsing — caller gets the raw concatenated
        text blocks. Used for short, free-form generation tasks
        (funder-submission narrative, etc.) where the response is a few
        sentences and doesn't need structured-output extraction.

        Does NOT retry. Callers that need retry semantics wrap with their
        own tenacity decorator; the funder-narrative path returns empty
        on any failure rather than blocking the dossier render.
        """
        response = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return _text_blocks(response)

    @retry(
        retry=retry_if_exception(_is_retryable_bedrock_error),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def invoke_tool_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tool_name: str,
        tool_schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
    ) -> tuple[dict[str, Any], str]:
        """Forced tool-use call returning the model's structured JSON output.

        The model is constrained by ``tool_choice={"type":"tool","name":...}``
        so the response is guaranteed to be a single ``tool_use`` block whose
        ``input`` matches ``tool_schema``. Returns ``(tool_input, model_id)``
        — the second element is the exact model id the call used, persisted
        on the consumer's audit trail (so a stored summary is self-describing
        when the inference profile is later rotated).

        Used by ``aegis.scoring_v2.narrator`` for the plain-English deal
        summary. Tool-use envelope shape per Anthropic Messages API:
        https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/overview

        Same retry posture as ``classify_batch_json`` — 3 attempts on
        429 / 5xx / network. Deterministic failures (BadRequest,
        ValidationError) are NOT retried.

        Raises ``ValueError`` when the model returns a response that
        does not contain a single ``tool_use`` block (defensive — the
        forced ``tool_choice`` makes this nearly impossible, but a
        future SDK change could surface a different shape).
        """
        response = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            tools=[
                {
                    "name": tool_name,
                    "description": (
                        "Emit the structured output for the deal-summary "
                        "narrator. Required — do not respond outside this tool."
                    ),
                    "input_schema": tool_schema,
                }
            ],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": user_prompt}],
        )
        tool_block: Any | None = None
        for block in getattr(response, "content", []):
            block_type = getattr(block, "type", None)
            block_name = getattr(block, "name", None)
            if block_type == "tool_use" and block_name == tool_name:
                tool_block = block
                break
        if tool_block is None:
            raise ValueError(
                f"Bedrock response did not include expected tool_use block for tool '{tool_name}'"
            )
        tool_input = getattr(tool_block, "input", None)
        if not isinstance(tool_input, dict):
            raise ValueError(f"tool_use.input was not a dict (got {type(tool_input).__name__})")
        return tool_input, self._model

    def invoke_with_web_search(self, prompt: str, *, max_uses: int = 5) -> str:
        """Send the prompt with the ``web_search_20250305`` server tool enabled.

        Returns the model's final text response as one concatenated string —
        ``_text_blocks`` skips tool-use / tool-result blocks so the caller
        gets only the natural-language answer.

        Used by ``aegis.web_presence.scanner`` for reputation lookups. The
        scanner catches every exception and returns an empty result, so
        this method intentionally does NOT retry: the retry budget belongs
        to the operator's explicit refresh, not to the failure path.

        ``max_uses`` caps how many web-search round-trips Anthropic can
        run server-side per invocation. Five is enough for a reputation
        sketch without burning unbounded search credits.
        """
        response = self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            tools=[
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": max_uses,
                }
            ],
            messages=[{"role": "user", "content": prompt}],
        )
        return _text_blocks(response)


def _text_blocks(response: object) -> str:
    """Concatenate all text blocks from an Anthropic Messages response."""
    pieces: list[str] = []
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "text":
            pieces.append(block.text)
    return "\n".join(pieces)


def _first_json_object(text: str) -> dict[str, Any]:
    """Extract the first {...} JSON object in a text response.

    Uses `JSONDecoder.raw_decode` so trailing commentary or a second JSON
    object after the first one is harmlessly ignored — Bedrock occasionally
    emits the schema object followed by prose or a duplicated object, and
    the previous `find('{')` ... `rfind('}')` slice would span both and
    raise `JSONDecodeError: Extra data`. The first balanced object wins.

    If the response parses to a top-level JSON array, raise a clear error
    pointing at `parser/prompts.py` so operators know the prompt — not the
    parser — is the place to fix the schema mismatch.
    """
    start = text.find("{")
    bracket_start = text.find("[")
    # If the response is a bare array (no object wrapper), give a clearer
    # error than "no JSON object". This lands in `aegis.parser.extract`
    # logs as `LLM returned malformed JSON: ...`, and the operator's first
    # action should be to inspect / amend the prompt.
    if start == -1 and bracket_start != -1:
        raise ValueError(
            "LLM returned a top-level JSON array; expected an object. "
            "Fix the prompt in `aegis/parser/prompts.py` to require an "
            'object wrapper (e.g. {"classifications": [...]}).'
        )
    if start == -1:
        raise ValueError(f"no JSON object in LLM response: {text[:200]!r}")
    decoder = json.JSONDecoder()
    try:
        parsed, _end = decoder.raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        snippet = text[start : start + 200]
        raise ValueError(f"could not parse JSON: {exc}; raw={snippet!r}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            "top-level JSON is not an object: got "
            f"{type(parsed).__name__}. Fix the prompt in "
            "`aegis/parser/prompts.py` to require an object wrapper."
        )
    return parsed


__all__ = ["BedrockClient", "LLMClient"]
