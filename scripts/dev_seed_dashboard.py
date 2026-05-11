"""Dev-only: pre-seed the in-memory dashboard with a corpus statement.

Demonstrates the full findings panel — score breakdown, stacking card,
pattern flags — without needing a live AWS Bedrock pass. Uses
``manifest-feed mode`` (same path as ``tests/test_corpus.py``): reads
the corpus manifest as the ground-truth extraction, runs the
deterministic validate → classify → aggregate → score chain, and
injects the result into ``InMemoryDocumentRepository``.

Usage:

    AEGIS_DATA_RESIDENCY_CONFIRMED=true \
    AEGIS_STORAGE_BACKEND=memory \
    API_BEARER_TOKEN=dev-test-token \
    python -m scripts.dev_seed_dashboard \
        --manifest tests/fixtures/corpus/synthetic/mca_stacked_chase_business_10003.manifest.json \
        --port 5556

Once the server is up, open ``/ui/merchants/{id}`` to see the populated
panel. The script prints the merchant URL on stdout.

This is the synthetic-corpus demo path. For REAL bank statements the
operator drops a PDF into ``/ui/upload`` and the arq worker (with AWS
Bedrock credentials) parses it through the same pipeline.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4


def _seed_and_serve(manifest_path: Path, port: int) -> None:
    # Set env BEFORE importing aegis — config has a boot guard.
    os.environ.setdefault("AEGIS_DATA_RESIDENCY_CONFIRMED", "true")
    os.environ.setdefault("AEGIS_STORAGE_BACKEND", "memory")
    os.environ.setdefault("API_BEARER_TOKEN", "dev-test-token")

    from aegis.api.app import create_app
    from aegis.api.deps import (
        get_merchant_repository,
        get_repository,
    )
    from aegis.merchants.models import MerchantRow
    from aegis.merchants.repository import InMemoryMerchantRepository
    from aegis.parser.aggregate import aggregate
    from aegis.parser.extract import ExtractionPass1Result
    from aegis.parser.metadata import MetadataAnalysis
    from aegis.parser.models import (
        ClassifiedTransaction,
        ExtractedStatement,
        StatementSummary,
        ValidationResult,
    )
    from aegis.parser.patterns import analyze_patterns
    from aegis.parser.pipeline import PipelineResult
    from aegis.parser.validate import validate_extraction
    from aegis.storage import InMemoryDocumentRepository

    manifest = json.loads(manifest_path.read_text())
    pdf_path = manifest_path.with_suffix("").with_suffix(".pdf")
    if not pdf_path.exists():
        sys.exit(f"PDF not found next to manifest: {pdf_path}")

    # 1. Reconstruct the extracted statement from the manifest.
    summary_raw = manifest["summary"]
    summary = StatementSummary(
        beginning_balance=Decimal(summary_raw["beginning_balance"]),
        ending_balance=Decimal(summary_raw["ending_balance"]),
        deposit_total=Decimal(summary_raw["deposit_total"]),
        withdrawal_total=Decimal(summary_raw["withdrawal_total"]),
        period_start=date.fromisoformat(summary_raw["period_start"]),
        period_end=date.fromisoformat(summary_raw["period_end"]),
        printed_transaction_count=summary_raw.get("printed_transaction_count"),
    )
    classified = [
        ClassifiedTransaction(
            posted_date=date.fromisoformat(t["posted_date"]),
            description=t["description"],
            amount=Decimal(t["amount"]),
            running_balance=Decimal(t["running_balance"]) if t.get("running_balance") else None,
            source_page=t["source_page"],
            source_line=t["source_line"],
            category=t["category"],
            classification_confidence=100,
        )
        for t in manifest["transactions"]
    ]

    # 2. Run the deterministic pipeline pieces.
    extraction = ExtractedStatement(summary=summary, transactions=list(classified))
    validation = validate_extraction(extraction)
    if not validation.passed:
        sys.exit(f"manifest validation failed: {validation.failures}")
    aggregates = aggregate(
        classified,
        period_start=summary.period_start,
        period_end=summary.period_end,
        beginning_balance=summary.beginning_balance,
    ).aggregates
    patterns = analyze_patterns(
        classified,
        period_start=summary.period_start,
        period_end=summary.period_end,
    )

    # 3. Wrap into a PipelineResult for storage.
    metadata = MetadataAnalysis(
        pdf_creation_date=None,
        pdf_modification_date=None,
        pdf_producer=None,
        pdf_creator=None,
        pdf_author=None,
        page_count=1,
        file_size_bytes=pdf_path.stat().st_size,
        eof_markers=1,
        page_sizes=["Letter"],
        flags=[],
        fraud_score=0,
    )
    expected = manifest.get("expected", {})
    fraud_max = expected.get("fraud_score", {}).get("max", 30)
    fraud_min = expected.get("fraud_score", {}).get("min", 0)
    fraud_score = (fraud_max + fraud_min) // 2 if isinstance(fraud_max, int) else 30
    flags: list[str] = [f"[PATTERN] {p}" for p in patterns.flags]
    rec = expected.get("recommendation", "approve")
    status: str
    if rec == "decline":
        status = "manual_review"
    elif rec == "refer":
        status = "review"
    else:
        status = "proceed"

    result = PipelineResult(
        parse_status=status,  # type: ignore[arg-type]
        metadata=metadata,
        extraction=ExtractionPass1Result(
            statement=extraction, synthetic_risk_indicators=[], truncated=False
        ),
        validation=ValidationResult(passed=True, failures=[], warnings=[]),
        classified=list(classified),
        patterns=patterns,
        aggregates=aggregates,
        fraud_score=fraud_score,
        fraud_score_breakdown={"manifest_demo": fraud_score},
        all_flags=flags,
    )

    # 4. Build app + in-memory stores + dependency overrides BEFORE serve.
    merchants_repo = InMemoryMerchantRepository()
    docs_repo = InMemoryDocumentRepository()

    merchant = MerchantRow(
        business_name=f"Demo — {manifest['scenario']}",
        owner_name="Corpus Operator",
        state="CA",
        entity_type="llc",
        requested_amount=Decimal("75000"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
        broker_source="Corpus Seed",
        intake_date=date(2026, 5, 10),
        is_renewal=False,
        credit_score=720,
        time_in_business_months=36,
    )
    merchants_repo.upsert(merchant)

    doc_row = docs_repo.create_document(
        file_hash="seed-" + uuid4().hex,
        byte_size=pdf_path.stat().st_size,
        original_filename=pdf_path.name,
        uploaded_by="dev-seed",
        merchant_id=merchant.id,
    )
    docs_repo.persist_parse_result(
        doc_row.id, result=result, merchant_id=merchant.id
    )

    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchants_repo
    app.dependency_overrides[get_repository] = lambda: docs_repo

    print(f"\n  Seeded merchant:  {merchant.business_name}")
    print(f"  Scenario:         {manifest['scenario']}")
    print(f"  Open in browser:  http://127.0.0.1:{port}/ui/merchants/{merchant.id}")
    print(f"  Or CSV download:  http://127.0.0.1:{port}/ui/merchants/{merchant.id}/findings.csv\n")

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=port)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("tests/fixtures/corpus/synthetic/mca_stacked_chase_business_10003.manifest.json"),
        help="Path to a corpus manifest JSON.",
    )
    parser.add_argument("--port", type=int, default=5556)
    args = parser.parse_args()
    _seed_and_serve(args.manifest, args.port)


if __name__ == "__main__":
    main()
