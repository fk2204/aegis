"""Bedrock per-deal cost tracking (mp Phase 11 task #2).

Two surfaces:

* **In-line accounting**: ``CostTrackingBedrockClient`` wraps the
  production ``BedrockClient`` and records ``input_tokens`` +
  ``output_tokens`` + computed USD cost into ``audit_log`` after every
  successful Bedrock call. The wrapper preserves the ``LLMClient``
  Protocol so callers swap it in without touching code paths.
* **Weekly digest**: ``build_weekly_digest`` reads recent
  ``bedrock.usage`` rows from ``audit_log``, groups by document, and
  emits a structured ``WeeklyDigest`` carrying:
    - total tokens + cost per deal
    - average cost per deal
    - cost per funded deal (when override outcome marks it)
    - cost as % of revenue (when revenue is known via deal data)

Pricing is configured via env so a model migration is a config change,
not a code change. Defaults match the AWS Bedrock list price for
``us.anthropic.claude-sonnet-4-6`` as of 2026-05-19.

Audit row shape
---------------
Every Bedrock call writes one row with::

    {
      "actor": "bedrock.client",
      "action": "bedrock.usage",
      "subject_type": "document"    (when document_id is supplied)
      "subject_id":   <document_id> (when document_id is supplied)
      "details": {
        "operation":        "extract|extract_vision|classify",
        "input_tokens":     <int>,
        "output_tokens":    <int>,
        "input_cost_usd":   "0.0030",   (Decimal serialized as string)
        "output_cost_usd":  "0.0015",
        "total_cost_usd":   "0.0045",
        "model_id":         "us.anthropic.claude-sonnet-4-6"
      }
    }

USD prices are stored as ``Decimal`` strings so the weekly digest can
reconstruct exact totals without ``float`` arithmetic.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from aegis.audit import AuditLog, AuditWriteError
from aegis.config import get_settings
from aegis.llm import BedrockClient
from aegis.logger import get_logger
from aegis.ops.llm_cost_repository import CallType, LLMCostRepository

_log = get_logger(__name__)


# Method-name → CallType inference used when the wrapper isn't given an
# explicit call_type. The non-Protocol methods (generate_text /
# invoke_with_web_search / invoke_tool_json) DO require explicit
# call_type because they have no single canonical category.
_DEFAULT_CALL_TYPE_BY_OPERATION: dict[str, CallType] = {
    "extract": "extraction",
    "extract_vision": "extraction",
    "classify": "classification",
}


# --- pricing (env-tunable) -------------------------------------------------

#: USD per 1M input tokens, default matches Claude Sonnet 4.6 list price
#: on us.anthropic.claude-sonnet-4-6 (2026-05-19). Override via env.
_DEFAULT_INPUT_USD_PER_MTOK = Decimal("3.00")

#: USD per 1M output tokens, default matches Claude Sonnet 4.6 list price.
_DEFAULT_OUTPUT_USD_PER_MTOK = Decimal("15.00")


def _input_price() -> Decimal:
    raw = os.environ.get("AEGIS_BEDROCK_INPUT_USD_PER_MTOK")
    return Decimal(raw) if raw else _DEFAULT_INPUT_USD_PER_MTOK


def _output_price() -> Decimal:
    raw = os.environ.get("AEGIS_BEDROCK_OUTPUT_USD_PER_MTOK")
    return Decimal(raw) if raw else _DEFAULT_OUTPUT_USD_PER_MTOK


def compute_cost_usd(*, input_tokens: int, output_tokens: int) -> Decimal:
    """Per-call cost in USD as ``Decimal``. Quantized to 6 decimal places.

    Quantization is intentionally fine-grained — a single classify-pass
    call costs fractions of a cent, and aggregating thousands of them
    over a week needs to retain precision. Money columns elsewhere use
    14,2; the weekly digest converts back to 14,2 for display.
    """
    inp = Decimal(input_tokens) * _input_price() / Decimal(1_000_000)
    out = Decimal(output_tokens) * _output_price() / Decimal(1_000_000)
    return (inp + out).quantize(Decimal("0.000001"))


# --- wrapper client --------------------------------------------------------


@dataclass
class _UsageRecord:
    """Per-call usage tally. Mutated by the wrapper, drained on flush."""

    input_tokens: int = 0
    output_tokens: int = 0
    operation: str = ""


class CostTrackingBedrockClient:
    """Wraps ``BedrockClient`` and writes a ``bedrock.usage`` audit row per call.

    The wrapper preserves the three-method ``LLMClient`` Protocol so
    callers (the parser pipeline) don't notice the substitution. It
    holds a reference to an ``AuditLog`` so each call writes the row
    synchronously — if the audit write fails, we log + continue
    (audit-write failure must NOT block the Bedrock call's caller;
    the Bedrock cost is already incurred by then).

    Note on ``document_id``: the Bedrock client itself doesn't know
    which document a call belongs to. The parser pipeline carries
    that information. Construction time is the natural plumbing point —
    the worker creates one wrapper per ``parse_document`` job with the
    document_id pinned, and the wrapper tags every audit row with
    ``subject_type="document"`` / ``subject_id=document_id``. Callers
    that lack a document context (ad-hoc scripts, OFAC refresh, etc.)
    pass ``document_id=None``; those rows still land in the digest's
    overall totals but are excluded from the per-deal breakdown.
    """

    def __init__(
        self,
        inner: BedrockClient | None = None,
        *,
        audit: AuditLog,
        document_id: UUID | None = None,
        merchant_id: UUID | None = None,
        cost_repo: LLMCostRepository | None = None,
        call_type: CallType | None = None,
    ) -> None:
        self._inner = inner if inner is not None else BedrockClient()
        self._audit = audit
        self._model_id = get_settings().bedrock_model_id
        self._document_id = document_id
        self._merchant_id = merchant_id
        self._cost_repo = cost_repo
        # ``call_type`` is the *override* — when set it wins over the
        # method-name inference in ``_resolve_call_type``. Callers that
        # invoke a single category of work pass it at construction
        # (web-presence scanner, ucc checker, deal-summary narrator).
        # Workers that flow extract + classify through the same wrapper
        # leave it None so the per-method default kicks in.
        self._call_type_override = call_type

    # ----- LLMClient Protocol surface --------------------------------------

    def extract_raw_json(self, pdf_bytes: bytes, prompt: str) -> tuple[dict[str, Any], bool]:
        # We re-issue the streaming call here so we can capture
        # ``response.usage`` after the stream completes; the inner
        # ``BedrockClient.extract_raw_json`` doesn't return usage.
        return self._stream_with_usage(
            operation="extract",
            content=[
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": _b64(pdf_bytes),
                    },
                },
                {"type": "text", "text": prompt},
            ],
        )

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": _b64(png),
                },
            }
            for png in page_images_png
        ]
        content.append({"type": "text", "text": prompt})
        return self._stream_with_usage(operation="extract_vision", content=content)

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        # Classify uses a non-streaming create() call; mirror that here.
        response = self._inner._client.messages.create(
            model=self._inner._model,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        self._audit_usage("classify", response)
        from aegis.llm import _first_json_object, _text_blocks

        return _first_json_object(_text_blocks(response))

    # ----- internals -------------------------------------------------------

    def _stream_with_usage(
        self,
        *,
        operation: str,
        content: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        with self._inner._client.messages.stream(
            model=self._inner._model,
            max_tokens=64000,
            messages=[{"role": "user", "content": content}],  # type: ignore[typeddict-item]
        ) as stream:
            response = stream.get_final_message()
        self._audit_usage(operation, response)
        from aegis.llm import _first_json_object, _text_blocks

        truncated = getattr(response, "stop_reason", None) == "max_tokens"
        return _first_json_object(_text_blocks(response)), truncated

    def _resolve_call_type(self, operation: str) -> CallType | None:
        """Pick the CallType used for the llm_costs row.

        Explicit override wins. Otherwise fall back to the per-method
        default. ``invoke_with_web_search`` + ``invoke_tool_json`` + the
        plain ``generate_text`` have NO inferable default — those
        callers must construct with ``call_type=...`` set. Returns
        ``None`` when no resolution is possible; the dual-write skips
        the llm_costs insert in that case (audit_log row still lands).
        """
        if self._call_type_override is not None:
            return self._call_type_override
        return _DEFAULT_CALL_TYPE_BY_OPERATION.get(operation)

    def _audit_usage(self, operation: str, response: object) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            _log.warning(
                "bedrock.usage.missing operation=%s — response.usage absent; "
                "cost tracking row skipped",
                operation,
            )
            return
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cost = compute_cost_usd(input_tokens=input_tokens, output_tokens=output_tokens)
        details = {
            "operation": operation,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_cost_usd": str(
                (Decimal(input_tokens) * _input_price() / Decimal(1_000_000)).quantize(
                    Decimal("0.000001")
                )
            ),
            "output_cost_usd": str(
                (Decimal(output_tokens) * _output_price() / Decimal(1_000_000)).quantize(
                    Decimal("0.000001")
                )
            ),
            "total_cost_usd": str(cost),
            "model_id": self._model_id,
        }
        subject_type = "document" if self._document_id is not None else None
        try:
            self._audit.record(
                actor="bedrock.client",
                action="bedrock.usage",
                subject_type=subject_type,
                subject_id=self._document_id,
                details=details,
            )
        except AuditWriteError:
            _log.warning("bedrock.usage.audit_failed operation=%s", operation)

        # Dual-write: also persist into llm_costs when the repo is
        # wired AND we can resolve a CallType. The Bedrock cost is
        # already incurred; a failed insert must NOT propagate (the
        # audit_log row is the canonical record either way).
        if self._cost_repo is None:
            return
        call_type = self._resolve_call_type(operation)
        if call_type is None:
            _log.warning(
                "bedrock.usage.call_type_unresolved operation=%s — "
                "llm_costs row skipped (audit_log row still written)",
                operation,
            )
            return
        try:
            self._cost_repo.insert(
                merchant_id=self._merchant_id,
                document_id=self._document_id,
                model_id=self._model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=cost,
                call_type=call_type,
            )
        except Exception:
            _log.warning("bedrock.usage.cost_repo_failed operation=%s", operation, exc_info=True)

    # ----- non-Protocol surface --------------------------------------------
    #
    # The 3 methods below let callers that today instantiate ``BedrockClient``
    # directly (``web_presence/scanner.py``, ``business_intel/ucc_checker.py``,
    # ``scoring_v2/deal_summary.py``, narrator's tool-use path) flow through
    # the same cost-tracking decorator. They forward to the inner client and
    # then call ``_audit_usage`` so the dual-write happens uniformly.

    def generate_text(self, prompt: str, *, max_tokens: int = 512) -> str:
        """Mirror :meth:`BedrockClient.generate_text` with cost recording."""
        response = self._inner._client.messages.create(
            model=self._inner._model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        self._audit_usage("generate_text", response)
        from aegis.llm import _text_blocks

        return _text_blocks(response)

    def invoke_with_web_search(self, prompt: str, *, max_uses: int = 5) -> str:
        """Mirror :meth:`BedrockClient.invoke_with_web_search` with cost
        recording, plus the 2026-07-01 graceful fallback for
        ``web_search_20250305`` deprecation. See the underlying
        ``BedrockClient.invoke_with_web_search`` docstring for the
        rationale."""
        try:
            response = self._inner._client.messages.create(
                model=self._inner._model,
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
        except Exception as exc:
            msg = str(exc)
            if "web_search_20250305" in msg and "does not match any of the expected tags" in msg:
                # Fall through to the prompt-only path so the caller's
                # timestamp still lands. Cost tracking on the fallback
                # comes through the wrapped ``generate_text`` call the
                # inner method dispatches to.
                return self._inner.invoke_prompt_only(prompt, max_tokens=2048)
            raise
        self._audit_usage("invoke_with_web_search", response)
        from aegis.llm import _text_blocks

        return _text_blocks(response)

    def invoke_prompt_only(self, prompt: str, *, max_tokens: int = 512) -> str:
        """Mirror :meth:`BedrockClient.invoke_prompt_only` with cost recording."""
        response = self._inner._client.messages.create(
            model=self._inner._model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        self._audit_usage("invoke_prompt_only", response)
        from aegis.llm import _text_blocks

        return _text_blocks(response)

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
        """Mirror :meth:`BedrockClient.invoke_tool_json` with cost recording."""
        response = self._inner._client.messages.create(
            model=self._inner._model,
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
        self._audit_usage("invoke_tool_json", response)
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
        return tool_input, self._inner._model


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


# --- weekly digest ---------------------------------------------------------


@dataclass(frozen=True)
class PerDealCost:
    """One deal's Bedrock spend for the digest window.

    ``document_id`` is the document the call was for (None when the
    call was made outside any document context — e.g. an OFAC refresh).
    ``revenue`` is the merchant's monthly revenue when known (filled
    by the digest builder from the analyses table); None when not
    available.
    """

    document_id: UUID | None
    input_tokens: int
    output_tokens: int
    total_cost_usd: Decimal
    revenue: Decimal | None = None
    call_count: int = 0
    funded: bool = False  # True iff the operator marked this deal funded


@dataclass(frozen=True)
class WeeklyDigest:
    """The §21 task #2 unit-economics shape.

    Per-deal rows are sorted by total_cost_usd descending so the
    operator's eyes land on the most-expensive deals first.
    """

    window_start: str  # ISO timestamp
    window_end: str
    total_calls: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: Decimal
    deals: list[PerDealCost] = field(default_factory=list)

    @property
    def avg_cost_per_deal(self) -> Decimal:
        deals_with_subject = [d for d in self.deals if d.document_id is not None]
        if not deals_with_subject:
            return Decimal("0.00")
        return (
            sum((d.total_cost_usd for d in deals_with_subject), Decimal("0"))
            / Decimal(len(deals_with_subject))
        ).quantize(Decimal("0.0001"))

    @property
    def avg_cost_per_funded_deal(self) -> Decimal | None:
        funded = [d for d in self.deals if d.funded]
        if not funded:
            return None
        return (
            sum((d.total_cost_usd for d in funded), Decimal("0")) / Decimal(len(funded))
        ).quantize(Decimal("0.0001"))

    @property
    def cost_pct_of_revenue(self) -> Decimal | None:
        """Total Bedrock cost as a percentage of total revenue across the
        funded deals in the window. None when no funded deal carries
        revenue info.
        """
        funded_with_rev = [d for d in self.deals if d.funded and d.revenue is not None]
        if not funded_with_rev:
            return None
        total_cost = sum((d.total_cost_usd for d in funded_with_rev), Decimal("0"))
        # ``d.revenue is not None`` is narrowed by the comprehension above
        # but mypy can't track that — guard once more for the type checker.
        total_revenue = sum(
            (d.revenue for d in funded_with_rev if d.revenue is not None),
            Decimal("0"),
        )
        if total_revenue == 0:
            return None
        return (Decimal("100") * total_cost / total_revenue).quantize(Decimal("0.0001"))


def build_weekly_digest(
    usage_rows: list[dict[str, Any]],
    *,
    window_start: str,
    window_end: str,
    funded_document_ids: set[UUID] | None = None,
    revenue_by_document: dict[UUID, Decimal] | None = None,
) -> WeeklyDigest:
    """Roll up audit_log rows with action='bedrock.usage' into a digest.

    Arguments:
      usage_rows — rows from ``audit_log`` filtered to action ==
        'bedrock.usage' within the window. The caller is responsible
        for the SQL filter; this function works against the parsed
        result so it's unit-testable without a database.
      window_start, window_end — ISO timestamps copied into the digest.
      funded_document_ids — set of documents the operator marked
        funded during the window (looked up from the overrides table
        or, post-Phase-10, the funder_replies table). Empty set = no
        funded deals known.
      revenue_by_document — optional merchant-monthly-revenue lookup,
        used to compute cost as % of revenue.

    Rows missing ``subject_id`` are kept (so non-document calls show
    up in the totals) but excluded from per-deal averages.
    """
    funded_document_ids = funded_document_ids or set()
    revenue_by_document = revenue_by_document or {}

    by_doc: dict[UUID | None, _DealAccumulator] = defaultdict(_DealAccumulator)
    total_calls = 0
    total_input = 0
    total_output = 0
    total_cost = Decimal("0")

    for row in usage_rows:
        details: dict[str, Any] = row.get("details") or {}
        try:
            input_tokens = int(details.get("input_tokens", 0))
            output_tokens = int(details.get("output_tokens", 0))
            cost = Decimal(details.get("total_cost_usd", "0"))
        except (ValueError, TypeError):
            _log.warning("digest.skip_malformed_row action=%s", row.get("action"))
            continue

        document_id: UUID | None = None
        subj_id = row.get("subject_id")
        if subj_id and row.get("subject_type") == "document":
            try:
                document_id = UUID(str(subj_id))
            except ValueError:
                document_id = None

        acc = by_doc[document_id]
        acc.input_tokens += input_tokens
        acc.output_tokens += output_tokens
        acc.total_cost_usd += cost
        acc.call_count += 1

        total_calls += 1
        total_input += input_tokens
        total_output += output_tokens
        total_cost += cost

    deals: list[PerDealCost] = []
    for doc_id, acc in by_doc.items():
        deals.append(
            PerDealCost(
                document_id=doc_id,
                input_tokens=acc.input_tokens,
                output_tokens=acc.output_tokens,
                total_cost_usd=acc.total_cost_usd.quantize(Decimal("0.000001")),
                revenue=revenue_by_document.get(doc_id) if doc_id else None,
                call_count=acc.call_count,
                funded=(doc_id in funded_document_ids) if doc_id else False,
            )
        )
    deals.sort(key=lambda d: d.total_cost_usd, reverse=True)

    return WeeklyDigest(
        window_start=window_start,
        window_end=window_end,
        total_calls=total_calls,
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        total_cost_usd=total_cost.quantize(Decimal("0.000001")),
        deals=deals,
    )


@dataclass
class _DealAccumulator:
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: Decimal = Decimal("0")
    call_count: int = 0


def build_cost_tracking_client(
    *,
    call_type: CallType,
    document_id: UUID | None = None,
    merchant_id: UUID | None = None,
) -> CostTrackingBedrockClient:
    """Wire a :class:`CostTrackingBedrockClient` against the process-wide
    audit + llm_costs singletons.

    Convenience for callers that don't want to thread DI through their
    own signature — used by the lazy fallbacks in
    :mod:`aegis.web_presence.scanner`,
    :mod:`aegis.business_intel.ucc_checker`, and
    :mod:`aegis.scoring_v2.deal_summary` when no client is injected.
    Tests that need a wrapped client without the singletons construct
    ``CostTrackingBedrockClient`` directly with their own ``audit`` /
    ``cost_repo``.
    """
    # Imported lazily so this module stays importable without FastAPI.
    from aegis.api.deps import get_audit, get_llm_cost_repository

    return CostTrackingBedrockClient(
        audit=get_audit(),
        cost_repo=get_llm_cost_repository(),
        document_id=document_id,
        merchant_id=merchant_id,
        call_type=call_type,
    )


__all__ = [
    "CostTrackingBedrockClient",
    "PerDealCost",
    "WeeklyDigest",
    "build_cost_tracking_client",
    "build_weekly_digest",
    "compute_cost_usd",
]
