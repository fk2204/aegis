"""Parse aggregate-emitted soft-signal flag strings into structured dicts.

``aggregate.aggregate()`` returns flags like:

    "top_counterparty_concentration:43%_(acme_corp)"
    "payroll_cadence:biweekly_12%_of_revenue"
    "payroll_cadence:irregular_count_1"
    "nsf_on_negative_days:4_of_7"
    "adb_partial_coverage:3/30"

Persisted into ``DocumentRow.all_flags`` as opaque strings. This module
parses them back into structured dicts the merchant_detail soft-signals
card can render with proper units + explanatory copy. Keeps the storage
shape simple (single ``text[]`` column) and avoids a migration to add
columns nobody else reads yet.

If the parsing breaks because the aggregate emits a new flag format,
update ``parse_soft_signal_flags`` here in the same commit that lands
the format change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CustomerConcentration:
    top_payee: str
    share_pct: int


@dataclass(frozen=True)
class PayrollCadence:
    cadence: str  # "weekly" | "biweekly" | "monthly" | "irregular" | "irregular_count_1"
    pct_of_revenue: int | None  # None when revenue is zero or cadence is single-event


@dataclass(frozen=True)
class NSFNegativeOverlap:
    overlap: int
    total: int

    @property
    def ratio_pct(self) -> int:
        if self.total <= 0:
            return 0
        return round(100 * self.overlap / self.total)


@dataclass(frozen=True)
class ADBPartialCoverage:
    covered_days: int
    total_days: int


@dataclass(frozen=True)
class SoftSignalSummary:
    customer_concentration: CustomerConcentration | None = None
    payroll_cadence: PayrollCadence | None = None
    nsf_negative_overlap: NSFNegativeOverlap | None = None
    adb_partial_coverage: ADBPartialCoverage | None = None
    # Flags that look like soft signals but didn't match a known prefix
    # — surfaced for visibility but without operator-readable copy.
    unmapped: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return (
            self.customer_concentration is None
            and self.payroll_cadence is None
            and self.nsf_negative_overlap is None
            and self.adb_partial_coverage is None
            and not self.unmapped
        )


# Soft-signal codes recognised by the parser. Pattern codes from
# parser.patterns (e.g. "duplicate_deposits_detected") are intentionally
# excluded — those have their own pattern cards.
_SOFT_SIGNAL_PREFIXES: tuple[str, ...] = (
    "top_counterparty_concentration",
    "payroll_cadence",
    "nsf_on_negative_days",
    "adb_partial_coverage",
)

_CONCENTRATION_RE = re.compile(
    r"^top_counterparty_concentration:(?P<pct>\d+)%_\((?P<payee>[^)]*)\)$"
)
_PAYROLL_WITH_PCT_RE = re.compile(
    r"^payroll_cadence:(?P<cadence>[a-z_]+?)_(?P<pct>\d+)%_of_revenue$"
)
_PAYROLL_NO_PCT_RE = re.compile(r"^payroll_cadence:(?P<cadence>[a-z_0-9]+)$")
_NSF_OVERLAP_RE = re.compile(r"^nsf_on_negative_days:(?P<overlap>\d+)_of_(?P<total>\d+)$")
_ADB_PARTIAL_RE = re.compile(
    r"^adb_partial_coverage:(?P<covered>\d+)/(?P<total>\d+)$"
)


def parse_soft_signal_flags(all_flags: list[str]) -> SoftSignalSummary:
    """Return a SoftSignalSummary built from the flag strings the aggregate
    emits. Unknown flag prefixes or flags belonging to other subsystems are
    ignored silently.
    """
    cc: CustomerConcentration | None = None
    pr: PayrollCadence | None = None
    nsf: NSFNegativeOverlap | None = None
    adb: ADBPartialCoverage | None = None
    unmapped: list[str] = []

    for flag in all_flags:
        if not any(flag.startswith(prefix + ":") for prefix in _SOFT_SIGNAL_PREFIXES):
            continue

        m = _CONCENTRATION_RE.match(flag)
        if m:
            cc = CustomerConcentration(
                top_payee=m.group("payee").strip(),
                share_pct=int(m.group("pct")),
            )
            continue

        m = _PAYROLL_WITH_PCT_RE.match(flag)
        if m:
            pr = PayrollCadence(
                cadence=m.group("cadence"),
                pct_of_revenue=int(m.group("pct")),
            )
            continue

        m = _PAYROLL_NO_PCT_RE.match(flag)
        if m:
            pr = PayrollCadence(
                cadence=m.group("cadence"),
                pct_of_revenue=None,
            )
            continue

        m = _NSF_OVERLAP_RE.match(flag)
        if m:
            nsf = NSFNegativeOverlap(
                overlap=int(m.group("overlap")),
                total=int(m.group("total")),
            )
            continue

        m = _ADB_PARTIAL_RE.match(flag)
        if m:
            adb = ADBPartialCoverage(
                covered_days=int(m.group("covered")),
                total_days=int(m.group("total")),
            )
            continue

        unmapped.append(flag)

    return SoftSignalSummary(
        customer_concentration=cc,
        payroll_cadence=pr,
        nsf_negative_overlap=nsf,
        adb_partial_coverage=adb,
        unmapped=unmapped,
    )


__all__ = [
    "ADBPartialCoverage",
    "CustomerConcentration",
    "NSFNegativeOverlap",
    "PayrollCadence",
    "SoftSignalSummary",
    "parse_soft_signal_flags",
]
