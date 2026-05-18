"""State compliance matrix — YAML-backed, Pydantic v2, strict-mode loader.

This module is the **machine-readable single source of truth** described in
master plan §9.1. It reads ``docs/compliance/states.yaml`` at boot and
produces a typed ``StateMatrix`` covering all 50 states + DC.

Design constraints (master plan §11, §2):

- Strict-mode Pydantic v2: every model forbids extra fields. A typo in
  ``states.yaml`` fails the boot, never silently mis-routes a deal.
- Discriminated union on the ``tier`` field. Tier 1 carries the full
  compliance surface; Tier 2 is a watch-list stub; Tier 3 carries only
  the defensive-disclosure default. Pydantic validates the right shape
  for each tier — there is no way to "accidentally" load a Tier 3 with
  Tier 1 fields populated.
- Pure-Python loader: ``load_matrix(path)`` returns a ``StateMatrix`` or
  raises ``StateMatrixError`` on any validation / IO failure. No
  external side effects.
- This module is intentionally **separate** from the legacy
  ``aegis.compliance.states`` module. Both will co-exist while later
  master-plan phases (4, 5, 8) migrate consumers from the legacy table
  to this matrix.

This file is companion to ``router.py``, which consumes a loaded matrix
to produce per-deal routing decisions.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError


def _coerce_to_decimal(v: object) -> Decimal:
    """Coerce YAML scalars to Decimal for money fields.

    YAML has no native Decimal type. We accept Decimal (passthrough),
    int (lossless), and str (explicit). float is rejected — money never
    transits binary float, per CLAUDE.md mathematical-correctness rules.
    Used via ``Money`` annotated alias below so the rest of the matrix
    can stay strict-mode while money fields accept YAML-shaped inputs.
    """
    if isinstance(v, Decimal):
        return v
    if isinstance(v, bool):  # bool is an int subclass — reject before int check
        raise TypeError(f"money field cannot be bool: {v!r}")
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, str):
        return Decimal(v)
    raise TypeError(
        f"money field must be Decimal, int, or str; got {type(v).__name__}"
    )


# Money type alias for the matrix — same constraints as the original
# inline annotation, plus a BeforeValidator that converts YAML scalars to
# Decimal so strict mode at the model level still works.
Money = Annotated[
    Decimal,
    BeforeValidator(_coerce_to_decimal),
    Field(max_digits=14, decimal_places=2, gt=Decimal("0")),
]

DEFAULT_MATRIX_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "docs" / "compliance" / "states.yaml"
)


class StateMatrixError(RuntimeError):
    """Raised on any ``states.yaml`` load / validation failure.

    The matrix is legally load-bearing. Boot must fail closed rather than
    serve traffic against a half-validated regulation table.
    """


# --- Enums / literal types ---------------------------------------------------

# COJ values per master plan §8.5. ``banned`` = banned outright;
# ``banned_in_mca`` = banned only within MCA contracts (VA, TX);
# ``conditional`` = permitted with restrictions (NY: in-state residents only);
# ``permitted`` = no state-level restriction; ``void`` = voided by general law.
CojStatus = Literal[
    "banned",
    "banned_in_mca",
    "banned_consumer",
    "conditional",
    "permitted",
    "void",
]

# Auto-debit status per master plan §8.5. ``prohibited_without_first_priority_lien``
# is TX-specific and is the deal-killer surfaced by the router.
AutodebitStatus = Literal[
    "permitted",
    "prohibited_without_first_priority_lien",
]

BrokerAdvanceFeeStatus = Literal["permitted", "prohibited"]
ForumRestriction = Literal["none", "mandatory_in_state"]
AgEnforcementRisk = Literal["low", "medium", "high"]

# Product scopes covered by the state's CFDL. Closed-end loans, sales-based
# financing (MCA), factoring, open-end (revolver), lease, asset-based.
ProductScope = Literal[
    "sales_based",
    "closed_end",
    "open_end",
    "factoring",
    "lease",
    "asset_based",
]

AprMethod = Literal["reg_z_1026_22", "actuarial_reg_z", "none"]

# Per-state registration cadence. Annual is the most common; biennial /
# one-time are kept open for future Tier 1 promotions.
RegistrationRenewal = Literal[
    "annual",
    "annual_by_january_31",
    "annual_by_october_1",
    "biennial",
    "one_time",
]

# Bill-status enum for the Tier 2 watch list.
BillLikelihood = Literal["low", "medium", "high"]

# Quality enum for Tier 3 defensive-disclosure default.
Tier3PostureStatus = Literal["defensive_disclosure_only", "decline_until_audited"]


# --- Models -----------------------------------------------------------------


class _StrictModel(BaseModel):
    """Base class — every subclass forbids extras and trims whitespace.

    The matrix loader runs in strict mode: pydantic does not coerce
    strings to numbers and does not silently drop unknown keys. A typo
    in ``states.yaml`` reaches the operator at boot, not in production
    months later.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class HardDeclineRule(_StrictModel):
    """A hard-decline rule attached to a state's CFDL surface.

    Currently only Tier 1 surfaces declare hard-decline rules. The
    canonical example is TX HB 700's auto-debit prohibition
    (``tx_autodebit_without_first_priority_lien``). The rule code is
    stable and used by the router and audit log; the message is the
    operator-facing explanation.
    """

    code: str = Field(min_length=1, description='Stable rule identifier (e.g. "tx_autodebit")')
    message: str = Field(min_length=1, description="Operator-facing explanation")


class Overlays(_StrictModel):
    """Per-state overlay rules per master plan §8.5.

    Overlays are tier-independent — they fire regardless of CFDL tier.
    Required on every Tier 1 entry; optional for Tier 2/3 entries
    (where they record posture but do not gate the router).
    """

    coj: CojStatus
    autodebit: AutodebitStatus
    broker_advance_fee: BrokerAdvanceFeeStatus
    forum_restriction: ForumRestriction
    ag_enforcement_risk: AgEnforcementRisk


class Penalties(_StrictModel):
    """Per-state penalty structure per master plan §8.2."""

    enforcement_authority: str = Field(min_length=1)
    private_right_of_action: bool
    max_per_violation_usd: Money | None = None
    max_aggregate_usd: Money | None = None
    notes: str | None = Field(default=None, min_length=1, max_length=2000)


class Cfdl(_StrictModel):
    """The CFDL block for a Tier 1 state per master plan §9.1.

    Field names mirror the master-plan sample YAML. Effective dates are
    explicit; threshold is in USD; product scope is a list of enums.
    Optional fields (e.g. ``apr_method``, ``broker_registration``) carry
    sensible defaults where the underlying statute is silent.
    """

    statute: list[str] = Field(min_length=1)
    effective: date
    threshold_usd: Money | None = None
    # When True, registration / disclosure obligations apply regardless of
    # deal size. TX HB 700 sets this True for registration even though the
    # disclosure threshold is $1M.
    no_threshold: bool = False
    product_scope: list[ProductScope] = Field(min_length=1)
    apr_required: bool
    apr_method: AprMethod = "none"
    broker_registration: bool = False
    registration_authority: str | None = Field(default=None, min_length=1)
    registration_effective: date | None = None
    registration_renewal: RegistrationRenewal | None = None
    renewal_redisclosure: bool = False
    retention_years: int = Field(ge=0, le=10)
    template_path: str | None = Field(
        default=None,
        min_length=1,
        description="Path (repo-relative) to the locked Jinja template",
    )
    template_sha256: str | None = Field(
        default=None,
        description="Populated by the Phase 5 snapshot test, not by the loader",
    )
    notes: str | None = Field(default=None, min_length=1, max_length=4000)


class Tier1Regulation(_StrictModel):
    """Tier 1: CFDL enacted, disclosure required, AEGIS must serve."""

    tier: Literal[1] = 1
    name: str = Field(min_length=1)
    cfdl: Cfdl
    overlays: Overlays
    penalties: Penalties
    hard_decline_rules: list[HardDeclineRule] = Field(default_factory=list)
    last_reviewed: date


class Tier2Regulation(_StrictModel):
    """Tier 2: active pending bills — watch list.

    Carries only the posture metadata needed for the watch-list digest.
    Overlays and penalties are absent because the state has no MCA-
    specific surface yet; the router treats Tier 2 deals identically to
    Tier 3 until the state promotes.
    """

    tier: Literal[2] = 2
    name: str = Field(min_length=1)
    pending_bills: list[str] = Field(default_factory=list)
    likelihood: BillLikelihood = "low"
    notes: str | None = Field(default=None, min_length=1, max_length=2000)
    last_reviewed: date


class Tier3Regulation(_StrictModel):
    """Tier 3: no MCA-specific law; defensive disclosure only.

    Master plan §8.4: Tier 3 states are served (do not auto-decline) and
    AEGIS generates a defensive disclosure + persists the decision
    snapshot. ``ag_enforcement_risk`` is optional and flags states like
    MA, DE that are aggressive even without their own CFDL.
    """

    tier: Literal[3] = 3
    name: str = Field(min_length=1)
    posture: Tier3PostureStatus = "defensive_disclosure_only"
    ag_enforcement_risk: AgEnforcementRisk = "low"
    notes: str | None = Field(default=None, min_length=1, max_length=2000)
    last_reviewed: date


StateRegulation = Annotated[
    Tier1Regulation | Tier2Regulation | Tier3Regulation,
    Field(discriminator="tier"),
]


class WatchlistEntry(_StrictModel):
    """A bill on the master-plan §8.3 Tier 2 watch list.

    Surfaces as the input to the §22 monthly review digest.
    """

    bills: list[str] = Field(min_length=1)
    likelihood: BillLikelihood
    notes: str | None = Field(default=None, min_length=1, max_length=2000)
    last_reviewed: date


class Tier3Default(_StrictModel):
    """Defaults for Tier 3 states (master plan §8.4)."""

    generate_defensive_disclosure: bool
    persist_decision_snapshot: bool
    flag_ag_enforcement_states: list[str] = Field(default_factory=list)


class StateMatrix(_StrictModel):
    """Top-level matrix object — the validated form of ``states.yaml``.

    ``states`` keys must be USPS 2-letter codes (50 states + DC = 51
    entries). Strict mode + the loader's post-validation make any
    deviation fail the boot.
    """

    version: str = Field(min_length=1, description='Matrix version, e.g. "2026.05.17"')
    states: dict[str, StateRegulation]
    watchlist: dict[str, WatchlistEntry] = Field(default_factory=dict)
    tier_3_default: Tier3Default


# --- Loader ------------------------------------------------------------------

# Canonical USPS 2-letter codes for the 50 states + DC. Required by the
# loader's post-validation step — any deviation in ``states.yaml`` raises.
_REQUIRED_STATE_CODES: Final[frozenset[str]] = frozenset(
    {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
        "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
        "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
        "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
        "DC",
    }
)


def _verify_state_coverage(matrix: StateMatrix) -> None:
    """Ensure ``states.yaml`` covers 50 states + DC, no extras.

    Raises ``StateMatrixError`` listing every missing or extra code so the
    operator can fix the matrix in one pass rather than discovering
    drift incrementally.
    """
    present = set(matrix.states.keys())
    missing = _REQUIRED_STATE_CODES - present
    extra = present - _REQUIRED_STATE_CODES
    problems: list[str] = []
    for code in sorted(missing):
        problems.append(f"missing state: {code}")
    for code in sorted(extra):
        problems.append(f"unknown state code: {code}")
    # Each state's key must match its USPS code uppercase.
    for code in sorted(present):
        if code != code.upper() or (len(code) != 2 and code != "DC"):
            problems.append(f"state key not normalized uppercase 2-letter: {code!r}")
    if problems:
        raise StateMatrixError(
            "states.yaml failed coverage validation:\n  - " + "\n  - ".join(problems)
        )


def load_matrix(path: Path | None = None) -> StateMatrix:
    """Load and validate ``states.yaml`` at the given path.

    Returns the validated ``StateMatrix``. Raises ``StateMatrixError`` on
    any IO / parse / validation failure. The default path is the canonical
    location under ``docs/compliance/states.yaml`` — pass ``path`` only
    when running mutation tests against a temp copy.
    """
    actual_path = path or DEFAULT_MATRIX_PATH
    try:
        with actual_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise StateMatrixError(f"states.yaml not found at {actual_path}") from exc
    except yaml.YAMLError as exc:
        raise StateMatrixError(f"states.yaml parse failure: {exc}") from exc

    if not isinstance(raw, dict):
        raise StateMatrixError(
            f"states.yaml root must be a mapping; got {type(raw).__name__}"
        )

    try:
        matrix = StateMatrix.model_validate(raw)
    except ValidationError as exc:
        # Render Pydantic errors as a flat indented list to make boot
        # logs readable; multi-error YAMLs are common during edits.
        lines = [
            f"  - {'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        ]
        raise StateMatrixError(
            "states.yaml failed schema validation:\n" + "\n".join(lines)
        ) from exc

    _verify_state_coverage(matrix)
    return matrix


__all__ = [
    "DEFAULT_MATRIX_PATH",
    "AgEnforcementRisk",
    "AprMethod",
    "AutodebitStatus",
    "BillLikelihood",
    "BrokerAdvanceFeeStatus",
    "Cfdl",
    "CojStatus",
    "ForumRestriction",
    "HardDeclineRule",
    "Overlays",
    "Penalties",
    "ProductScope",
    "RegistrationRenewal",
    "StateMatrix",
    "StateMatrixError",
    "StateRegulation",
    "Tier1Regulation",
    "Tier2Regulation",
    "Tier3Default",
    "Tier3PostureStatus",
    "Tier3Regulation",
    "WatchlistEntry",
    "load_matrix",
]
