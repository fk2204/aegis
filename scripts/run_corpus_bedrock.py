"""Real-LLM corpus runner for Bedrock verification gate.

Walks `tests/fixtures/corpus/synthetic/*.pdf`, runs the full
`run_pipeline` against each via a token-counting BedrockClient
proxy, and writes a JSON results file. Intended for the Hetzner
box where AWS creds live.

Run twice — once with `AEGIS_PARSER_PAGE_ROUTING=0` (baseline) and
once with `AEGIS_PARSER_PAGE_ROUTING=1` (new path) — then diff the
two JSON files with `compare_corpus_runs.py`.

Exits non-zero if any document fails to match its manifest's
expected validation_passed flag.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from aegis.config import get_settings  # noqa: E402
from aegis.llm import BedrockClient  # noqa: E402
from aegis.parser.pipeline import run_pipeline  # noqa: E402

CORPUS_ROOT = REPO_ROOT / "tests" / "fixtures" / "corpus" / "synthetic"


@dataclass
class DocResult:
    pdf: str
    expected_validation_passed: bool
    actual_parse_status: str
    actual_validation_passed: bool
    passed: bool
    input_tokens: int
    output_tokens: int
    page_strategies: list[str]
    flags: list[str]
    elapsed_seconds: float


class _CountingBedrockClient:
    """Wraps BedrockClient to record per-call token usage.

    Token counts are exposed through the underlying anthropic
    Messages response.usage; we re-issue the raw streaming call here
    so we can capture both .usage and the original payload.
    """

    def __init__(self) -> None:
        self._inner = BedrockClient()
        self.input_tokens_total = 0
        self.output_tokens_total = 0

    def _capture_usage(self, response: object) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.input_tokens_total += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens_total += int(getattr(usage, "output_tokens", 0) or 0)

    def extract_raw_json(
        self, pdf_bytes: bytes, prompt: str
    ) -> tuple[dict[str, Any], bool]:
        import base64

        with self._inner._client.messages.stream(
            model=self._inner._model,
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
        self._capture_usage(response)
        from aegis.llm import _first_json_object, _text_blocks

        truncated = getattr(response, "stop_reason", None) == "max_tokens"
        return _first_json_object(_text_blocks(response)), truncated

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
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
        with self._inner._client.messages.stream(
            model=self._inner._model,
            max_tokens=64000,
            messages=[{"role": "user", "content": content}],  # type: ignore[typeddict-item]
        ) as stream:
            response = stream.get_final_message()
        self._capture_usage(response)
        from aegis.llm import _first_json_object, _text_blocks

        truncated = getattr(response, "stop_reason", None) == "max_tokens"
        return _first_json_object(_text_blocks(response)), truncated

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        response = self._inner._client.messages.create(
            model=self._inner._model,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        self._capture_usage(response)
        from aegis.llm import _first_json_object, _text_blocks

        return _first_json_object(_text_blocks(response))


def _load_manifest_expected(pdf: Path) -> bool:
    """Return whether the manifest expects validation to pass.

    Defaults to True if no expected.validation_passed key is set
    (i.e. clean scenarios).
    """
    manifest_path = pdf.with_suffix(".manifest.json")
    if not manifest_path.exists():
        return True
    with manifest_path.open("r", encoding="utf-8") as f:
        manifest: dict[str, Any] = json.load(f)
    expected = manifest.get("expected", {})
    return bool(expected.get("validation_passed", True))


def _discover_pdfs() -> list[Path]:
    return sorted(CORPUS_ROOT.glob("*.pdf"))


def _per_doc(pdf: Path) -> DocResult:
    llm = _CountingBedrockClient()
    started = time.monotonic()
    result = run_pipeline(str(pdf), llm)
    elapsed = time.monotonic() - started

    expected = _load_manifest_expected(pdf)
    actual_passed = bool(result.validation and result.validation.passed)
    parse_status = result.parse_status

    # In manifest-feed mode, tampered/decline scenarios are expected to
    # fail validation. Real-LLM mode: if expected=True we want passed,
    # if expected=False we want manual_review (i.e. NOT passed).
    matches = actual_passed == expected

    page_strategies: list[str] = []
    settings = get_settings()
    if settings.aegis_parser_page_routing:
        # Surface routing decisions if available; parser logs them via
        # logger.info but the result object doesn't carry them. We
        # re-classify here purely for the report.
        from aegis.parser.page_router import classify_pages

        decisions = classify_pages(str(pdf))
        page_strategies = [d.strategy for d in decisions]

    return DocResult(
        pdf=pdf.name,
        expected_validation_passed=expected,
        actual_parse_status=parse_status,
        actual_validation_passed=actual_passed,
        passed=matches,
        input_tokens=llm.input_tokens_total,
        output_tokens=llm.output_tokens_total,
        page_strategies=page_strategies,
        flags=list(result.all_flags),
        elapsed_seconds=round(elapsed, 2),
    )


def _summarize(results: Iterable[DocResult]) -> dict[str, Any]:
    results = list(results)
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    total_input = sum(r.input_tokens for r in results)
    total_output = sum(r.output_tokens for r in results)
    total_pages = sum(len(r.page_strategies) for r in results)
    text_pages = sum(
        sum(1 for s in r.page_strategies if s == "text") for r in results
    )
    return {
        "total_docs": total,
        "passed_docs": passed,
        "failed_docs": total - passed,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "avg_input_tokens_per_doc": round(total_input / max(total, 1)),
        "avg_output_tokens_per_doc": round(total_output / max(total, 1)),
        "total_pages_classified": total_pages,
        "text_strategy_pages": text_pages,
        "text_strategy_pct": (
            round(100 * text_pages / max(total_pages, 1), 2)
            if total_pages
            else None
        ),
        "page_routing_enabled": get_settings().aegis_parser_page_routing,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="JSON output path for per-doc results.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="If set, only run the first N corpus PDFs (for smoke).",
    )
    args = parser.parse_args()

    pdfs = _discover_pdfs()
    if args.limit:
        pdfs = pdfs[: args.limit]
    if not pdfs:
        print(f"No PDFs found under {CORPUS_ROOT}", file=sys.stderr)
        return 2

    flag = os.environ.get("AEGIS_PARSER_PAGE_ROUTING", "0")
    print(f"AEGIS_PARSER_PAGE_ROUTING={flag} — {len(pdfs)} PDFs")
    print(f"BEDROCK_MODEL_ID={os.environ.get('BEDROCK_MODEL_ID', '<unset>')}")

    results: list[DocResult] = []
    for i, pdf in enumerate(pdfs, 1):
        try:
            r = _per_doc(pdf)
        except Exception as exc:
            print(f"  [{i:02d}/{len(pdfs)}] {pdf.name}  ERROR: {exc}")
            results.append(
                DocResult(
                    pdf=pdf.name,
                    expected_validation_passed=_load_manifest_expected(pdf),
                    actual_parse_status="error",
                    actual_validation_passed=False,
                    passed=False,
                    input_tokens=0,
                    output_tokens=0,
                    page_strategies=[],
                    flags=[f"runner_error:{type(exc).__name__}"],
                    elapsed_seconds=0.0,
                )
            )
            continue
        mark = "PASS" if r.passed else "FAIL"
        print(
            f"  [{i:02d}/{len(pdfs)}] {pdf.name}  {mark} "
            f"in:{r.input_tokens} out:{r.output_tokens} "
            f"pages:{len(r.page_strategies)} status:{r.actual_parse_status}"
        )
        results.append(r)

    summary = _summarize(results)
    out = {
        "summary": summary,
        "results": [r.__dict__ for r in results],
    }
    args.out.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print()
    print(f"Wrote {args.out}")
    print(json.dumps(summary, indent=2))

    return 0 if summary["failed_docs"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
