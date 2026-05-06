"""LLM client wrapper.

All Claude calls go through AWS Bedrock — never the direct Anthropic API.
The `BedrockClient` is the production implementation. The `LLMClient`
Protocol exists so tests can inject a fake without touching boto3 / AWS.

Boot guard: importing this module triggers `get_settings()` so the
data-residency check runs before any LLM call can be issued. Independently,
`BedrockClient.__init__` re-checks that the model id starts with "us." —
defense in depth in case settings get mutated.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import anthropic

from aegis.config import DataResidencyError, get_settings


class LLMClient(Protocol):
    """Minimal interface the parser needs from the LLM.

    Two methods, mapping to the two-pass parser flow.
    Tests provide stubs implementing this Protocol.
    """

    def extract_raw_json(self, pdf_bytes: bytes, prompt: str) -> dict[str, Any]:
        """Run the extraction pass: PDF in, raw JSON out (pre-Pydantic validation)."""

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

    def extract_raw_json(self, pdf_bytes: bytes, prompt: str) -> dict[str, Any]:
        """Send a PDF document block + extraction prompt; parse the first JSON object out."""
        import base64

        response = self._client.messages.create(
            model=self._model,
            max_tokens=16384,
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
        )
        return _first_json_object(_text_blocks(response))

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
    """Extract the first {...} JSON object in a text response."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object in LLM response: {text[:200]!r}")
    snippet = text[start : end + 1]
    try:
        parsed = json.loads(snippet)
    except json.JSONDecodeError as exc:
        raise ValueError(f"could not parse JSON: {exc}; raw={snippet[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"top-level JSON is not an object: got {type(parsed).__name__}")
    return parsed


__all__ = ["BedrockClient", "LLMClient"]
