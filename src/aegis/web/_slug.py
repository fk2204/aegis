"""ASCII-safe slug helper.

Used for ``content-disposition`` filenames in CSV downloads, ZIP entry
names in the submission flow, and any other place a user-supplied
business / funder name needs to become a filename-safe token.
"""

from __future__ import annotations


def slugify(text: str) -> str:
    """Return an ASCII-safe slug. Empty / non-alnum input falls back to ``"merchant"``."""
    out: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "_":
            out.append("_")
    return "".join(out).strip("_") or "merchant"


__all__ = ["slugify"]
