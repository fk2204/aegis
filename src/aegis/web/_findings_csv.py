"""Flatten ``MerchantFindings`` → CSV bytes.

Stdlib ``csv`` only — no new dependency. Output is a multi-section CSV
with blank-line separators between sections so it imports cleanly into
Excel and stays diff-friendly:

  1. Header (generated_at + generator_version)
  2. Merchant intake (column-per-field, EIN excluded)
  3. Compliance ribbon (state tier, OFAC, renewal)
  4. Documents (one row per parsed document)
  5. Latest score breakdown (per-factor delta rows)
  6. Latest stacking summary (single row)

EIN is masked at the source (``MerchantFindings.merchant`` already
omits it), so we never write it here.
"""

from __future__ import annotations

import csv
import io

from aegis.api.routes.findings import MerchantFindings


def findings_to_csv(findings: MerchantFindings) -> str:
    """Return a CSV string of the findings payload.

    Caller wraps in ``Response`` with the right ``content-disposition``.
    """
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)

    writer.writerow(["section", "key", "value"])
    writer.writerow(["meta", "generated_at", findings.generated_at.isoformat()])
    writer.writerow(["meta", "generator_version", findings.generator_version])
    writer.writerow([])

    writer.writerow(["section", "field", "value"])
    for k, v in _merchant_rows(findings):
        writer.writerow(["merchant", k, _render(v)])
    writer.writerow([])

    writer.writerow(["section", "field", "value"])
    writer.writerow(["compliance", "state_tier", findings.compliance.state_tier])
    writer.writerow(["compliance", "ofac_status", findings.compliance.ofac_status])
    writer.writerow(
        ["compliance", "ofac_match", _render(findings.compliance.ofac_match)]
    )
    writer.writerow(["compliance", "is_renewal", findings.compliance.is_renewal])
    writer.writerow([])

    writer.writerow(
        [
            "section",
            "document_id",
            "parse_status",
            "fraud_score",
            "uploaded_at",
            "period_start",
            "period_end",
            "days",
            "true_revenue",
            "avg_daily_balance",
            "lowest_balance",
            "num_nsf",
            "days_negative",
            "mca_positions",
            "mca_daily_total",
            "debt_to_revenue",
            "payroll_detected",
            "flags",
        ]
    )
    for d in findings.documents:
        writer.writerow(
            [
                "document",
                str(d.document_id),
                d.parse_status,
                _render(d.fraud_score),
                d.uploaded_at.isoformat(),
                _render(d.statement_period_start),
                _render(d.statement_period_end),
                _render(d.statement_days),
                _render(d.true_revenue),
                _render(d.avg_daily_balance),
                _render(d.lowest_balance),
                _render(d.num_nsf),
                _render(d.days_negative),
                _render(d.mca_positions),
                _render(d.mca_daily_total),
                _render(d.debt_to_revenue),
                _render(d.payroll_detected),
                "; ".join(d.flags),
            ]
        )
    writer.writerow([])

    if findings.latest_score is not None:
        s = findings.latest_score
        writer.writerow(["section", "field", "value"])
        writer.writerow(["score", "tier", s.tier])
        writer.writerow(["score", "score", s.score])
        writer.writerow(["score", "recommendation", s.recommendation])
        writer.writerow(["score", "suggested_max_advance", _render(s.suggested_max_advance)])
        writer.writerow(["score", "recommended_factor_rate", _render(s.recommended_factor_rate)])
        writer.writerow(["score", "recommended_holdback_pct", _render(s.recommended_holdback_pct)])
        writer.writerow(["score", "estimated_payback_days", _render(s.estimated_payback_days)])
        writer.writerow(["score", "apr", _render(s.apr)])
        writer.writerow(["score", "hard_decline_reasons", "; ".join(s.hard_decline_reasons)])
        writer.writerow(["score", "soft_concerns", "; ".join(s.soft_concerns)])
        writer.writerow([])

        writer.writerow(["section", "factor", "delta"])
        for entry in s.breakdown:
            writer.writerow(
                ["score_breakdown", entry.get("factor", ""), entry.get("delta", "")]
            )
        writer.writerow([])

    if findings.stacking is not None:
        writer.writerow(["section", "field", "value"])
        writer.writerow(["stacking", "daily_total", _render(findings.stacking.daily_total)])
        writer.writerow(["stacking", "monthly_burden", _render(findings.stacking.monthly_burden)])
        writer.writerow(["stacking", "position_count", findings.stacking.position_count])
        writer.writerow(["stacking", "debit_count", findings.stacking.debit_count])

    return buf.getvalue()


def _merchant_rows(findings: MerchantFindings) -> list[tuple[str, object]]:
    m = findings.merchant
    return [
        ("id", m.id),
        ("business_name", m.business_name),
        ("dba", m.dba),
        ("owner_name", m.owner_name),
        ("state", m.state),
        ("industry_naics", m.industry_naics),
        ("industry_risk_tier", m.industry_risk_tier),
        ("entity_type", m.entity_type),
        ("time_in_business_months", m.time_in_business_months),
        ("credit_score", m.credit_score),
        ("requested_amount", m.requested_amount),
        ("requested_factor", m.requested_factor),
        ("requested_term_days", m.requested_term_days),
        ("broker_source", m.broker_source),
        ("intake_date", m.intake_date),
        ("is_renewal", m.is_renewal),
    ]


def _render(value: object) -> str:
    if value is None:
        return ""
    return str(value)


__all__ = ["findings_to_csv"]
