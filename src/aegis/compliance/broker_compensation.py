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
from typing import Final, Protocol

from aegis.logger import get_logger

_log = get_logger(__name__)


class BrokerCompensationDisclosureMissing(RuntimeError):
    """Base class for per-state broker-compensation disclosure failures.

    Subclasses set ``citation`` to the controlling regulation. The base
    class exists so callers can ``except BrokerCompensationDisclosureMissing``
    once across all covered states.
    """

    citation: Final[str] = ""

    def __init__(self, message: str) -> None:
        super().__init__(f"{message} ({self.citation})")


class NyBrokerCompensationDisclosureMissing(BrokerCompensationDisclosureMissing):
    """Raised when an NY-merchant disclosure lacks broker compensation text.

    Cite: 23 NYCRR § 600.21(f).
    """

    citation: Final[str] = "23 NYCRR § 600.21(f)"


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


__all__ = [
    "BrokerCompensationDisclosureMissing",
    "NyBrokerCompensationDisclosureMissing",
    "validate_broker_compensation_disclosure",
]
