"""Image-only / scanned-style synthetic bank statement generator (Phase 11).

The default synthetic corpus from ``scripts/generate_corpus.py`` produces
text-bearing PDFs — every page yields a text layer that pymupdf
classifies as ``text`` strategy in ``parser.page_router``. That is fine
for end-to-end validation but it means ``compare_corpus_runs.py``'s
token-reduction signal cannot tell whether the page-routing
optimization is doing its job, because there is never a vision branch
to take. Verified 2026-05-19 verify-bedrock run: 56/56 pass, 100%
TEXT-mode pages, -1.18% token delta (per-page classifier overhead with
no offsetting savings).

This script wraps the existing text generator and produces a small set
of **image-only** PDFs that mirror the same scenarios. Each output PDF
is created by:

  1. Building a ``SyntheticStatement`` with a fixed seed (re-uses
     ``generate_corpus._build_statement`` so the manifest matches the
     numbers exactly).
  2. Rendering the statement to a normal text-bearing PDF in-memory.
  3. Rasterizing every page via pymupdf at 200 DPI into PNGs.
  4. Writing a new PDF that contains only the rasterized PNGs (no
     text layer). ``pymupdf.Page.insert_image`` over a fresh page
     produces a PDF whose ``get_text("text")`` returns ``""`` — the
     page-router classifies these as ``vision`` strategy with high
     vision_confidence.

Output paths
------------
PDFs and manifests are written to
``tests/fixtures/corpus/synthetic/image_only_<scenario>_<bank>_<seed>.pdf``
plus a matching ``.manifest.json``. The ``image_only_`` prefix makes
them distinguishable from clean-only docs by ``compare_corpus_runs.py``
without breaking the existing ``clean_profitable_*`` selection logic.

Hard rule (testing.md): manifests for synthetic PDFs are computed from
the same scenario builders as the corresponding text PDF — they are
NOT extracted from the LLM's output. Determinism is preserved by the
fixed-seed builder.

Usage
-----
Run from repo root after the regular generator has been run::

    python -m scripts.generate_image_only_corpus
    python -m scripts.generate_image_only_corpus --clean
    python -m scripts.generate_image_only_corpus --dry-run

The script never deletes the original text-bearing synthetic corpus —
``--clean`` only deletes ``image_only_*`` files.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pymupdf

from scripts.generate_corpus import (
    _BANK_LAYOUTS,
    CORPUS_DIR,
    MANIFEST_VERSION,
    SyntheticStatement,
    _build_statement,
    _render_pdf,
)
from scripts.generate_corpus import Recipe as _BaseRecipe

# Image-only files are written into the same directory as text-bearing
# synthetic PDFs. The ``image_only_`` prefix is the discriminator.
IMAGE_ONLY_PREFIX: Final[str] = "image_only_"

# Rasterization DPI for the image-only build. Matches the OCR-fallback
# DPI used by the parser pipeline so the input the LLM sees in real-LLM
# corpus runs is the same shape as what production would receive on an
# actual scanned statement.
RASTER_DPI: Final[int] = 200


@dataclass(frozen=True)
class ImageOnlyRecipe:
    """One image-only PDF to produce.

    ``source_seed`` is the seed used by ``generate_corpus._build_statement``
    so the manifest content matches the same numbers the corresponding
    text-bearing PDF would have. We pick a small subset of scenarios
    that exercise the page-routing branch meaningfully — clean
    profitable, NSF heavy, MCA stacked. Three is enough to make the
    aggregate token-reduction signal meaningful without bloating the
    corpus.
    """

    bank: str
    scenario: str
    source_seed: int
    image_only_seed: int  # used only for the output filename suffix


# Three image-only recipes. The scenarios chosen all pass validation in
# the existing text-bearing corpus (clean_profitable, mca_stacked,
# nsf_heavy), so their image-only counterparts exercise the vision
# extraction path against a known-good ground truth. Tampered /
# math_tampered scenarios are deliberately excluded — they already
# manual_review on the text path and adding a vision counterpart adds
# no signal.
IMAGE_ONLY_RECIPES: Final[tuple[ImageOnlyRecipe, ...]] = (
    ImageOnlyRecipe(
        bank="chase_business",
        scenario="clean_profitable",
        source_seed=10001,
        image_only_seed=90001,
    ),
    ImageOnlyRecipe(
        bank="boa_business",
        scenario="mca_stacked",
        source_seed=20003,
        image_only_seed=90002,
    ),
    ImageOnlyRecipe(
        bank="wells_fargo_business",
        scenario="nsf_heavy",
        source_seed=30002,
        image_only_seed=90003,
    ),
)


def _render_to_image_only_bytes(stmt: SyntheticStatement) -> bytes:
    """Render ``stmt`` to PDF then convert to image-only bytes.

    The roundtrip is intentional: we keep the layout exact (same bank
    header, same summary box, same transaction table) but strip the
    text layer entirely. Pages come back as embedded PNGs only.
    """
    layout = _BANK_LAYOUTS[stmt.bank]

    # Render text PDF to a temp file, then read it back into pymupdf.
    # We cannot pass an io.BytesIO directly to reportlab's
    # ``canvas.Canvas`` and re-read because reportlab's ``invariant``
    # mode is only well-tested against on-disk paths.
    with io.BytesIO() as tmp_buf:
        # Reportlab's canvas writes to a path-like; use a real temp
        # path for invariance, then read bytes back. The cost is one
        # extra disk round-trip per scenario at generation time only.
        import tempfile

        with tempfile.NamedTemporaryFile(
            suffix=".pdf", delete=False
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
        try:
            _render_pdf(stmt, layout, tmp_path)
            tmp_buf.write(tmp_path.read_bytes())
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        text_pdf_bytes = tmp_buf.getvalue()

    # Rasterize every page of the text PDF at RASTER_DPI.
    src = pymupdf.open(stream=text_pdf_bytes, filetype="pdf")
    try:
        out_doc = pymupdf.open()
        try:
            for i in range(src.page_count):
                page = src.load_page(i)
                # 200 DPI ≈ 2.78x default 72-DPI page area.
                zoom = RASTER_DPI / 72.0
                matrix = pymupdf.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                png_bytes = pix.tobytes("png")

                # New page sized to the original page rect (in PDF
                # points). The embedded image fills the page exactly,
                # so the layout still looks like a real scanned doc
                # when opened in a viewer. No text layer is written.
                new_page = out_doc.new_page(
                    width=page.rect.width, height=page.rect.height
                )
                new_page.insert_image(page.rect, stream=png_bytes)
            # ``no_new_id=1`` suppresses pymupdf's per-save random PDF
            # ``/ID`` trailer entry. Without it, every regeneration
            # produces byte-different PDFs (random /ID changes), which
            # violates the corpus determinism contract from
            # ``.claude/rules/testing.md``: "two consecutive --clean
            # runs produce identical SHA-256 hashes". Verified
            # 2026-05-19: with no_new_id=1, three runs produced the
            # same sha256 for all three image-only PDFs.
            pdf_bytes: bytes = out_doc.tobytes(deflate=True, garbage=4, no_new_id=1)
            return pdf_bytes
        finally:
            out_doc.close()
    finally:
        src.close()


def _build_image_only_statement(recipe: ImageOnlyRecipe) -> SyntheticStatement:
    """Build the underlying SyntheticStatement for ``recipe``.

    Reuses ``generate_corpus._build_statement`` with the source seed so
    the manifest values are byte-identical to the corresponding
    text-bearing scenario. The output PDF file name then uses
    ``image_only_seed`` (90001+) so it's distinguishable on disk.
    """
    base_recipe = _BaseRecipe(
        bank=recipe.bank,
        scenario=recipe.scenario,
        seed=recipe.source_seed,
    )
    stmt = _build_statement(base_recipe)
    return stmt


def _write_image_only_pair(
    recipe: ImageOnlyRecipe, stmt: SyntheticStatement, out_dir: Path
) -> tuple[Path, Path]:
    slug = f"{IMAGE_ONLY_PREFIX}{recipe.scenario}_{recipe.bank}_{recipe.image_only_seed:05d}"
    pdf_path = out_dir / f"{slug}.pdf"
    manifest_path = out_dir / f"{slug}.manifest.json"

    pdf_bytes = _render_to_image_only_bytes(stmt)
    pdf_path.write_bytes(pdf_bytes)

    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "scenario": stmt.scenario,
        "bank": stmt.bank,
        "seed": recipe.image_only_seed,
        "summary": {
            "beginning_balance": str(stmt.beginning_balance),
            "ending_balance": str(stmt.ending_balance),
            "deposit_total": str(stmt.deposit_total),
            "withdrawal_total": str(stmt.withdrawal_total),
            "period_start": stmt.period_start.isoformat(),
            "period_end": stmt.period_end.isoformat(),
            "printed_transaction_count": len(stmt.transactions),
        },
        "transactions": [t.to_manifest() for t in stmt.transactions],
        "expected": stmt.expected,
        "tolerances": {"money": "1.00", "fraud_score": 5},
        # Phase 11 task #6 marker — the corpus runner doesn't read this
        # but verify-bedrock + compare_corpus_runs.py can branch on
        # filename prefix to compute the mixed-modality token-reduction
        # signal cleanly. Documenting the intent here keeps the manifest
        # honest about why the PDF has no text layer.
        "image_only": True,
        "image_only_source_seed": recipe.source_seed,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return pdf_path, manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clean",
        action="store_true",
        help=(
            "Delete existing image_only_* PDFs + manifests in the synthetic "
            "corpus dir before regenerating. Does NOT touch the text-bearing "
            "synthetic corpus."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan, write nothing.",
    )
    args = parser.parse_args(argv)

    out_dir = CORPUS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.clean:
        for f in out_dir.iterdir():
            name = f.name
            if not name.startswith(IMAGE_ONLY_PREFIX):
                continue
            if f.suffix in {".pdf", ".json"}:
                f.unlink()

    print(f"output: {out_dir}")
    for recipe in IMAGE_ONLY_RECIPES:
        stmt = _build_image_only_statement(recipe)
        if args.dry_run:
            print(
                f"  PLAN  {IMAGE_ONLY_PREFIX}{recipe.scenario}_{recipe.bank}"
                f"_{recipe.image_only_seed:05d}: "
                f"{len(stmt.transactions)} transactions, image-only"
            )
            continue
        pdf_path, manifest_path = _write_image_only_pair(recipe, stmt, out_dir)
        print(
            f"  WROTE {pdf_path.name} ({pdf_path.stat().st_size}B), "
            f"{manifest_path.name}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
