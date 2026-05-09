# Broker Advertising Rules — Operational Checklist

Per-state requirements that govern AEGIS's marketing copy, landing
pages, sales scripts, social media posts, and any other public-facing
material soliciting business in that state. These rules are operational
(content review at design time), not runtime checks — the matcher does
not enforce them at deal time. The operator must run marketing copy
through this checklist before publishing in any covered state.

---

## Florida — Fla. Stat. § 559.9614(3)

**Rule.** Any advertisement of broker services directed at Florida
businesses must include:

1. The actual address and telephone number of the broker's business.
2. The address and telephone number of any forwarding service the
   broker uses (if applicable).

**Source.** `docs/compliance/03_florida.md`, "Broker rules under
FCFDL (§ 559.9614)" → item 3.

**Operational application.**

- AEGIS landing pages, contact pages, lead-gen funnels, and email
  signatures shown to FL merchants must display AEGIS's real business
  address and phone number — not a P.O. box, not a virtual office
  unless the virtual office's address and phone are themselves
  disclosed as the forwarding service.
- Any third-party lead-gen partner forwarding to AEGIS counts as a
  "forwarding service" — its address and phone must also appear.
- Cold email and SMS campaigns to FL merchants must carry the
  disclosure in-line, not just on a linked landing page.

**Enforcement.** Florida Attorney General (exclusive — no private
right of action). Penalty: $500 per violation, $20,000 aggregate
(initial); $1,000 per violation, $50,000 aggregate after written
notice of prior violation.

**Status.** Documented; no runtime enforcement. Operator action: add
the disclosure block to every FL-targeted marketing artifact before
publishing.

---

## Other states

No other Tier 1 state currently in AEGIS imposes a broker
advertising-disclosure rule of this shape. Update this file when:

- A new state is promoted with
  `broker_advertisement_address_disclosure_required=True` in
  `compliance/states.py`.
- Existing Tier 1 states amend their broker rules (e.g. NY CFDL
  amendments, GA SB 90 amendments).
