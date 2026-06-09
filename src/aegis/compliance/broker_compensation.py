"""Per-state broker-compensation disclosure guard.

Some states require the provider (funder) to inform the recipient
(merchant) in writing how, and by whom, the broker is compensated for
their role in the transaction. AEGIS is the broker; the funder is the
provider. AEGIS supplies the funder with a per-funder text block
describing the AEGIS↔funder compensation arrangement (commission %, ISO
fee schedule, etc.); the funder includes that block when transmitting
the standardized disclosure to the merchant.

States covered
--------------
* **New York** (CFDL).
  Source: ``docs/compliance/02_new_york.md`` → "Broker compensation:
  REQUIRED disclosure in NY (§ 600.21(f))":

      "When a broker is involved, the **provider must inform the
      recipient in writing how, and by whom, the broker is compensated**
      for their role in the transaction."

  Cite: 23 NYCRR § 600.21(f).

  Operational note: the disclosure of broker compensation is a
  **separate written communication** from the standardized § 600.6
  disclosure table — it travels alongside the disclosure rather than
  inside it. Brokerage fees treated as prepaid finance charges must
  ALSO be reflected in the finance-charge calculation per § 600.17 (a
  pricing concern, not a disclosure concern; handled by the APR engine).

California is **not** covered: the CA dossier records
``broker_compensation_disclosure_required: false``. § 952 transmission
duties are about forwarding the standardized disclosure unaltered, not
about compensation.

Implementation
--------------
A state code resolves to a ``_StateRule`` (citation + error class). For
any disclosure addressed to a covered state, the chosen funder must
have ``aegis_compensation_disclosure_text`` populated (non-empty after
strip). Empty text raises the state-specific error subclass with the
state's citation. Non-covered states pass through.

This is fail-closed: when in doubt, the disclosure pipeline must abort
rather than transmit a non-compliant package. The operator updates the
funder row with the agreed compensation text before retrying.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final, Protocol
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from aegis.compliance.states import TEMPLATES_DIR
from aegis.compliance.transmission import (
    DisclosureTransmissionRecord,
    DisclosureTransmissionRepository,
    record_disclosure_transmission,
)
from aegis.logger import get_logger

_log = get_logger(__name__)


# Class name follows the convention in ``compliance/states.py``
# (``StateNotServed``, ``StateNotAudited``) — exception classes describing a
# regulatory condition. Suppress the ruff "must end in Error" rule.
class BrokerCompensationDisclosureMissing(RuntimeError):  # noqa: N818
    """Base class for per-state broker-compensation disclosure failures.

    Subclasses set ``citation`` to the controlling regulation. The base
    class exists so callers can ``except BrokerCompensationDisclosureMissing``
    once across all covered states.
    """

    # Subclasses set this to their controlling statute / regulation.
    # Not ``Final`` on the base — subclass overrides are the whole point.
    citation: str = ""

    def __init__(self, message: str) -> None:
        super().__init__(f"{message} ({self.citation})")


class NyBrokerCompensationDisclosureMissing(BrokerCompensationDisclosureMissing):
    """Raised when an NY-merchant disclosure lacks broker compensation text.

    Cite: 23 NYCRR § 600.21(f).
    """

    citation: str = "23 NYCRR § 600.21(f)"


class _FunderLike(Protocol):
    """Minimal funder shape — anything carrying name + the disclosure text.

    Lets the guard accept ``FunderRow`` without importing it (keeps the
    compliance package's import graph free of funders).
    """

    name: str
    aegis_compensation_disclosure_text: str


@dataclass(frozen=True)
class _StateRule:
    error_cls: type[BrokerCompensationDisclosureMissing]
    state_label: str  # for log + error message disambiguation


_STATE_RULES: Final[dict[str, _StateRule]] = {
    "NY": _StateRule(
        error_cls=NyBrokerCompensationDisclosureMissing,
        state_label="NY",
    ),
}


def validate_broker_compensation_disclosure(
    *, merchant_state: str, funder: _FunderLike
) -> None:
    """Validate that the funder has on-file broker compensation text.

    Parameters
    ----------
    merchant_state
        USPS code of the merchant's principal place of business.
        Case-insensitive. Only states in the rule registry trigger the
        guard (currently NY).
    funder
        Any object with ``name`` and ``aegis_compensation_disclosure_text``
        attributes (typically a ``FunderRow``).

    Raises
    ------
    NyBrokerCompensationDisclosureMissing
        For NY merchants when the chosen funder's
        ``aegis_compensation_disclosure_text`` is empty / whitespace-only.

    Subclasses ``BrokerCompensationDisclosureMissing``; callers can
    ``except`` either the base or the specific subclass.
    """
    rule = _STATE_RULES.get((merchant_state or "").upper())
    if rule is None:
        return  # state has no broker-compensation disclosure rule.

    text = (funder.aegis_compensation_disclosure_text or "").strip()
    if text:
        return  # text on file — compliant.

    _log.warning(
        "broker_compensation.violation merchant_state=%s funder=%s",
        rule.state_label,
        funder.name,
    )
    raise rule.error_cls(
        f"{rule.state_label} merchant disclosure cannot be transmitted: "
        f"funder {funder.name!r} has no aegis_compensation_disclosure_text "
        "on file"
    )


# ---------------------------------------------------------------------------
# NY § 600.21(f) broker-compensation letter generator + transmission recorder
# ---------------------------------------------------------------------------
#
# R3.1 deliverable (per docs/AUDIT_2026_06_08 remediation plan): today
# this module only validated that the funder carried a broker-comp text
# block on file. The regulator-required broker-compensation LETTER —
# the separate written communication that travels alongside the
# standardized § 600.6 disclosure — was never actually generated. The
# pieces below produce the letter HTML, validate the inputs, and write
# the transmission audit row using the same disclosure_transmissions
# table the § 600.6 disclosure uses.
#
# AEGIS is the broker; the funder is the provider. Per the NY dossier
# (docs/compliance/02_new_york.md, "Broker compensation" section),
# AEGIS delivers the letter on behalf of the funder relationship.
# Recordkeeping is internal; the regulatory primary actor remains the
# funder.

# Disclosure version string written to the audit row. Distinct from
# NY_CFDL_v1 so an audit query can disambiguate letters from the
# § 600.6 sales-based-financing disclosure shipped in the same package.
NY_BROKER_COMP_DISCLOSURE_VERSION: Final[str] = "NY_BROKER_COMP_v1"

# Template path relative to ``compliance/templates/``. Stored as a
# module-level constant so callers, tests, and the audit row all share
# the same string.
NY_BROKER_COMP_TEMPLATE_NAME: Final[str] = "ny_broker_compensation_letter.html.j2"

# Stored on the audit row's ``template_path`` field so a future
# regulator-shaped audit query can locate the source template.
NY_BROKER_COMP_TEMPLATE_PATH: Final[str] = (
    "compliance/templates/ny_broker_compensation_letter.html.j2"
)

# Quantization helper. Letters carry money amounts at 2dp — same as
# the § 600.6 disclosure pipeline.
_CENT: Final[Decimal] = Decimal("0.01")


def _fmt_dollars(amount: Decimal) -> str:
    """Format a Decimal as ``"$X,XXX.XX"``. Always 2dp, comma-grouped."""
    quantized = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    return f"${quantized:,.2f}"


class BrokerCompensationLetterInputError(ValueError):
    """Raised when the letter context cannot satisfy § 600.21(f).

    Subclass of ``ValueError`` (not ``BrokerCompensationDisclosureMissing``):
    the missing-text guard is a per-state pre-flight, whereas this
    surface is a per-letter input validation failure (e.g. negative
    total compensation, missing recipient identifier). Keeping the two
    distinct lets the disclosure pipeline differentiate operator-fixable
    misconfiguration from a fundamental letter-input bug.
    """


class BrokerCompensationContext(BaseModel):
    """Inputs needed to render the NY § 600.21(f) broker comp letter.

    Pydantic so callers cannot pass loose dicts. Money fields use
    ``Decimal`` per AEGIS-wide rule (CLAUDE.md: "NEVER use float for
    money"). Both sub-components (paid_by_funder + paid_by_recipient)
    are required so an auditor can reconcile the split against the
    total — leaving either field implicit would lose the audit trail.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    broker_name: str
    # License / registration string. ``None`` is a legitimate state for
    # AEGIS today (Commera Capital operates as an ISO broker without a
    # state-issued license number) — the template omits the row when
    # ``broker_registration is None`` rather than rendering a blank.
    broker_registration: str | None
    funder_name: str
    recipient_name: str
    transaction_id: str
    letter_date: date
    total_compensation: Decimal
    compensation_paid_by_funder: Decimal
    compensation_paid_by_recipient: Decimal
    # Free-text description of any contingent / variable compensation
    # (e.g. "1% performance bonus payable if recipient remains in good
    # standing 90 days post-funding"). The template renders the string
    # verbatim. Use ``"None"`` when there is no contingent component.
    contingent_compensation_description: str

    @field_validator(
        "broker_name",
        "funder_name",
        "recipient_name",
        "transaction_id",
        "contingent_compensation_description",
    )
    @classmethod
    def _reject_empty(cls, v: str) -> str:
        if not v or not v.strip():
            # ValueError → Pydantic wraps in ValidationError → our
            # __init__ re-raises as BrokerCompensationLetterInputError
            # so callers can ``except`` the typed class once.
            raise ValueError(
                "field cannot be empty / whitespace-only — § 600.21(f) "
                "requires broker, funder, and recipient identification"
            )
        return v

    @field_validator("total_compensation")
    @classmethod
    def _total_must_be_non_negative(cls, v: Decimal) -> Decimal:
        if v < Decimal("0"):
            raise ValueError(
                f"total_compensation must be non-negative; got {v}"
            )
        return v

    @field_validator(
        "compensation_paid_by_funder", "compensation_paid_by_recipient"
    )
    @classmethod
    def _split_must_be_non_negative(cls, v: Decimal) -> Decimal:
        if v < Decimal("0"):
            raise ValueError(
                f"compensation split must be non-negative; got {v}"
            )
        return v

    def __init__(self, **data: Any) -> None:  # noqa: ANN401  # mirrors BaseModel.__init__'s **data signature
        # Re-raise Pydantic's ValidationError as the project-typed error
        # so callers can ``except BrokerCompensationLetterInputError``
        # without coupling to pydantic internals. Preserves the original
        # message + chain for debugging.
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise BrokerCompensationLetterInputError(str(exc)) from exc


# Module-level Jinja environment, mirroring disclosure.py's pattern.
# StrictUndefined + autoescape so a missing context variable raises
# rather than rendering an empty cell, and user input cannot inject
# HTML. The loader points at the same templates dir the § 600.6
# disclosure uses.
_LETTER_ENV: Final[Environment] = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(enabled_extensions=("html", "j2", "html.j2")),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


def render_broker_compensation_letter(context: BrokerCompensationContext) -> str:
    """Render the NY § 600.21(f) broker-compensation letter HTML.

    Parameters
    ----------
    context
        ``BrokerCompensationContext`` carrying broker, funder, recipient,
        and the compensation split. Money fields are quantized to 2dp
        and formatted as ``"$X,XXX.XX"`` for display.

    Returns
    -------
    str
        Rendered HTML, ready to email / archive / hash. The rendered
        bytes are the audit anchor (``html_sha256`` on the transmission
        row).

    Raises
    ------
    BrokerCompensationLetterInputError
        Already raised at the ``BrokerCompensationContext`` boundary —
        never silently degrades input.

    Notes
    -----
    The template's ``StrictUndefined`` policy means any context variable
    the template references must be present. New template variables
    require a corresponding context field; the test suite locks the
    rendered HTML via snapshot so unintentional drift surfaces in CI.
    """
    template = _LETTER_ENV.get_template(NY_BROKER_COMP_TEMPLATE_NAME)
    payload: dict[str, object] = {
        "broker_name": context.broker_name,
        "broker_registration": context.broker_registration,
        "funder_name": context.funder_name,
        "recipient_name": context.recipient_name,
        "transaction_id": context.transaction_id,
        "letter_date": context.letter_date.isoformat(),
        "total_compensation": _fmt_dollars(context.total_compensation),
        "compensation_paid_by_funder": _fmt_dollars(
            context.compensation_paid_by_funder
        ),
        "compensation_paid_by_recipient": _fmt_dollars(
            context.compensation_paid_by_recipient
        ),
        "contingent_compensation_description": (
            context.contingent_compensation_description
        ),
    }
    return template.render(**payload)


def record_broker_compensation_transmission(
    repo: DisclosureTransmissionRepository,
    context: BrokerCompensationContext,
    rendered_html: str,
    *,
    deal_id: UUID | None,
    merchant_id: UUID | None,
    recipient_email: str | None,
    sent_by: str | None,
    sent_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> DisclosureTransmissionRecord:
    """Write the broker-comp letter transmission to ``disclosure_transmissions``.

    Uses the existing R0.5 audit table — one row per letter delivered,
    keyed by the NY_BROKER_COMP_v1 ``disclosure_version`` so a regulator-
    or operator-shaped audit query can distinguish letters from the
    § 600.6 sales-based-financing disclosure. ``html_sha256`` is
    computed inside the helper from ``rendered_html``.

    The money fields the audit row exposes (``apr``, ``funding_provided``,
    ``finance_charge``, ``estimated_total_payment``, ``estimated_term_days``,
    ``factor_rate``, ``holdback_pct``) belong to the § 600.6 disclosure
    and are not meaningful for a broker-compensation letter — they are
    left ``None``. The broker-specific dollar amounts (total, funder
    split, recipient split, contingent description) live on
    ``metadata`` so the audit row carries the full broker-comp
    fact-set without expanding the table schema.

    Raises
    ------
    DisclosureTransmissionWriteError
        Propagated from the repository when the row cannot be persisted.
        The caller MUST halt rather than silently treating the letter
        as transmitted (4-year retention is meaningless if the row is
        dropped).
    """
    broker_metadata: dict[str, Any] = {
        "broker_name": context.broker_name,
        "broker_registration": context.broker_registration,
        "funder_name": context.funder_name,
        "transaction_id": context.transaction_id,
        "letter_date": context.letter_date.isoformat(),
        "total_compensation": str(
            context.total_compensation.quantize(_CENT, rounding=ROUND_HALF_UP)
        ),
        "compensation_paid_by_funder": str(
            context.compensation_paid_by_funder.quantize(
                _CENT, rounding=ROUND_HALF_UP
            )
        ),
        "compensation_paid_by_recipient": str(
            context.compensation_paid_by_recipient.quantize(
                _CENT, rounding=ROUND_HALF_UP
            )
        ),
        "contingent_compensation_description": (
            context.contingent_compensation_description
        ),
    }
    if metadata is not None:
        # Caller-supplied metadata wins on conflict — they're closer to
        # the calling context (e.g. funder-relationship id, deal-stage).
        broker_metadata.update(metadata)

    return record_disclosure_transmission(
        repo,
        deal_id=deal_id,
        merchant_id=merchant_id,
        state="NY",
        disclosure_version=NY_BROKER_COMP_DISCLOSURE_VERSION,
        template_path=NY_BROKER_COMP_TEMPLATE_PATH,
        rendered_html=rendered_html,
        recipient_email=recipient_email,
        sent_by=sent_by,
        apr=None,
        funding_provided=None,
        finance_charge=None,
        estimated_total_payment=None,
        estimated_term_days=None,
        factor_rate=None,
        holdback_pct=None,
        sent_at=sent_at,
        metadata=broker_metadata,
    )


# Belt-and-suspenders: the template file MUST live under TEMPLATES_DIR
# so the Jinja loader resolves ``NY_BROKER_COMP_TEMPLATE_NAME`` at
# render time. Catch the misconfiguration at import rather than at the
# first render request — saves an operator hours when the deploy
# packaged the wrong subset of files.
_TEMPLATE_FILE_PATH: Final[Path] = TEMPLATES_DIR / NY_BROKER_COMP_TEMPLATE_NAME
if not _TEMPLATE_FILE_PATH.is_file():  # pragma: no cover - install-time guard
    raise RuntimeError(
        f"missing template: {_TEMPLATE_FILE_PATH} — § 600.21(f) letter "
        "cannot be rendered. Reinstall the compliance/templates/ tree."
    )


__all__ = [
    "NY_BROKER_COMP_DISCLOSURE_VERSION",
    "NY_BROKER_COMP_TEMPLATE_NAME",
    "NY_BROKER_COMP_TEMPLATE_PATH",
    "BrokerCompensationContext",
    "BrokerCompensationDisclosureMissing",
    "BrokerCompensationLetterInputError",
    "NyBrokerCompensationDisclosureMissing",
    "record_broker_compensation_transmission",
    "render_broker_compensation_letter",
    "validate_broker_compensation_disclosure",
]
