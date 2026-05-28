"""Static guardrail tests that protect the no-transmission-to-funders rule.

AEGIS is a pre-screening tool — operators contact funders manually via
email / phone / portal. The codebase must never grow a path that
transmits deal data to a funder under any circumstance. These tests
catch the obvious vectors at CI time so a well-intentioned future
change doesn't silently introduce one.

Coverage:

1. No email-sending library is imported anywhere in ``src/aegis/``
   (smtplib, sendgrid, mailgun, aiosmtplib, ses_client wrappers, etc.).
2. Outbound HTTP destinations are restricted to a known allow-list of
   non-funder hosts. The allow-list keeps the security review tight: a
   net-new outbound host triggers an intentional decision rather than
   slipping in unnoticed.

If you genuinely need to add a new outbound dependency that talks to
something other than a funder (a new observability provider, an
unrelated SaaS), add the host to ``_ALLOWED_OUTBOUND_HOSTS`` in the
SAME commit with an explanatory comment. CI failing here is the prompt
for that decision — don't silence it.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src" / "aegis"


# Email-sending libraries we never want to see in this repo. Any of
# these would indicate someone is composing or sending an email from
# inside AEGIS — outside the no-transmission rule.
_BANNED_EMAIL_IMPORTS: tuple[str, ...] = (
    "smtplib",
    "aiosmtplib",
    "sendgrid",
    "mailgun",
    "boto3.client('ses')",  # AWS SES
    "boto3.client(\"ses\")",
)


# Outbound HTTP hosts we're OK with. Each entry is a substring matched
# against the URL host. New entries need both a code change AND a
# one-line justification here so the allow-list stays auditable.
#
# Categories:
#   endpoint    — code actually calls this URL at runtime
#   docref      — appears in a docstring / comment as a reference, no runtime call
#   citation    — state regulation URL stored as data in compliance/states.py
_ALLOWED_OUTBOUND_HOSTS: dict[str, str] = {
    # --- runtime endpoints --------------------------------------------------
    "api.close.com":                 "endpoint — Close CRM API (operator's CRM, not a funder)",
    "treas.gov":                     "endpoint — Treasury OFAC SDN list (sanctions check)",
    "ntfy.sh":                       "endpoint — ops push notifications",
    "hc-ping.com":                   "endpoint — healthchecks.io heartbeat pings",
    "127.0.0.1":                     "endpoint — localhost healthcheck (heartbeat_cli)",
    # --- documentation references (no runtime call) -------------------------
    "developer.close.com":           "docref — Close auth docs cited in close/client.py",
    "developers.cloudflare.com":     "docref — Cloudflare Access docs cited in ops/operators.py",
    "consumerfinance.gov":           "docref — CFPB Regulation Z citation in compliance/apr.py",
    # --- state regulation citations (data, not callers) ---------------------
    "leginfo.legislature.ca.gov":    "citation — CA statute reference (compliance/states.py)",
    "law.cornell.edu":               "citation — Cornell LII statute references",
    "law.justia.com":                "citation — Justia statute references",
    "dfpi.ca.gov":                   "citation — California DFPI regulation",
    "dfs.ny.gov":                    "citation — NY DFS regulation",
    "nysenate.gov":                  "citation — NY Senate bill text",
    "flsenate.gov":                  "citation — FL Senate statute",
    "legis.ga.gov":                  "citation — GA legislature bill text",
    "codes.findlaw.com":             "citation — FindLaw code references",
    "legiscan.com":                  "citation — Legiscan bill tracking",
    "ilga.gov":                      "citation — IL General Assembly",
    # Add new outbound hosts here. Each gets a one-line justification.
}


def _iter_python_files() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.py") if p.is_file())


def test_no_email_sending_libraries_imported() -> None:
    """Sanity guard: AEGIS never sends email. Any import of an email
    transport library is a regression in the no-transmission rule."""
    offending: list[tuple[Path, str]] = []
    for path in _iter_python_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for banned in _BANNED_EMAIL_IMPORTS:
            if banned in text:
                offending.append((path.relative_to(_REPO_ROOT), banned))
    assert not offending, (
        "Email transport library found in src/aegis — AEGIS must never send "
        "email to anyone, especially not funders. Hits:\n  "
        + "\n  ".join(f"{p}: {sym}" for p, sym in offending)
    )


# URL detector — explicit ``http://`` or ``https://`` scheme only. We do
# not try to flag bare-domain literals because Python module/attribute
# access (``aegis.api``, ``app.state``) has the same shape and would
# drown the test in false positives. The signal we care about is real
# outbound calls and URL strings, which always carry a scheme.
_URL_RE = re.compile(r"""https?://[^\s'"`<>)]+""", re.IGNORECASE)


def test_outbound_hosts_restricted_to_allowlist() -> None:
    """Any explicit ``http(s)://`` URL in src/aegis must map to a known
    non-funder destination. A net-new outbound host means a deliberate
    security decision — add it to ``_ALLOWED_OUTBOUND_HOSTS`` with a
    justification or remove the call."""
    findings: list[tuple[Path, str]] = []
    for path in _iter_python_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _URL_RE.finditer(text):
            raw = match.group(0).lower()
            # Strip the scheme + path so we compare hostnames only.
            host = raw.split("://", 1)[1].split("/", 1)[0]
            if any(allowed in host for allowed in _ALLOWED_OUTBOUND_HOSTS):
                continue
            findings.append((path.relative_to(_REPO_ROOT), match.group(0)))

    assert not findings, (
        "Outbound host(s) not in the allow-list. If this is a deliberate "
        "addition to a non-funder destination, add the host to "
        "_ALLOWED_OUTBOUND_HOSTS with a one-line justification. Hits:\n  "
        + "\n  ".join(f"{p}: {url}" for p, url in findings)
    )
