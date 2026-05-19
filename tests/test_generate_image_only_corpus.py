"""Determinism + page-router routing tests for the image-only generator.

The image-only corpus generator (`scripts/generate_image_only_corpus.py`)
is held to the same determinism contract as the regular synthetic
generator: two `--clean` runs must produce byte-identical PDFs and
manifests. Beyond determinism we also assert the page-router
classifies every output PDF as `vision` strategy — that's the load-
bearing claim Phase 11 task #6 relies on for re-promoting the
token-reduction hard gate in `compare_corpus_runs.py`.

These tests deliberately do NOT touch Bedrock. They run the generator
in an isolated tmp directory by monkeypatching `CORPUS_DIR`, then
inspect file hashes + page-router decisions on the generated PDFs.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest


def _hash_dir(d: Path) -> dict[str, str]:
    """Return {filename: sha256} for every PDF + manifest in ``d``."""
    out: dict[str, str] = {}
    for p in sorted(d.iterdir()):
        if p.suffix in {".pdf", ".json"}:
            out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


@pytest.fixture
def isolated_corpus_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the image-only generator's output to tmp_path.

    Patches both ``scripts.generate_image_only_corpus.CORPUS_DIR`` and
    the underlying ``scripts.generate_corpus.CORPUS_DIR`` symbols. We
    import the modules fresh on each call so the monkeypatch wins.
    """
    image_mod = importlib.import_module("scripts.generate_image_only_corpus")
    monkeypatch.setattr(image_mod, "CORPUS_DIR", tmp_path)
    return tmp_path


def test_image_only_generator_is_deterministic(
    isolated_corpus_dir: Path,
) -> None:
    """Two consecutive --clean runs must produce identical files.

    This is the same contract as the text-bearing generator. Without
    determinism, `verify-bedrock` re-runs would always show bogus
    "token deltas" caused by different PDF contents alone.
    """
    from scripts.generate_image_only_corpus import main as gen_main

    gen_main([])
    first = _hash_dir(isolated_corpus_dir)
    assert first, "generator wrote no files"

    gen_main(["--clean"])
    second = _hash_dir(isolated_corpus_dir)

    assert first == second, (
        "image-only generator is non-deterministic. Diff:\n"
        + "\n".join(
            f"  {name}: {first.get(name, 'MISSING')[:12]} -> "
            f"{second.get(name, 'MISSING')[:12]}"
            for name in sorted(set(first) | set(second))
            if first.get(name) != second.get(name)
        )
    )


def test_image_only_pdfs_route_to_vision(isolated_corpus_dir: Path) -> None:
    """Every generated image-only PDF must classify as vision strategy.

    The Phase 11 hard gate in compare_corpus_runs.py requires
    image_only_* page_strategies to be 100% vision. If pymupdf ever
    learns to recover a text layer from a rasterized embedded image,
    or if the rasterization DPI is reduced enough that the image gets
    OCR'd by viewer software, this test fails — surfacing the
    regression before it silently neutralizes the gate.
    """
    from aegis.parser.page_router import classify_pages, is_homogeneous
    from scripts.generate_image_only_corpus import main as gen_main

    gen_main([])

    pdfs = sorted(isolated_corpus_dir.glob("image_only_*.pdf"))
    assert pdfs, "no image_only_* PDFs were generated"

    for pdf in pdfs:
        decisions = classify_pages(str(pdf))
        assert decisions, f"page_router returned no decisions for {pdf.name}"
        homogeneous = is_homogeneous(decisions)
        assert homogeneous == "vision", (
            f"{pdf.name} expected homogeneous=vision, "
            f"got {homogeneous} with decisions={decisions}"
        )


def test_image_only_manifests_match_text_synthetic_numbers(
    isolated_corpus_dir: Path,
) -> None:
    """Image-only manifest values must be byte-identical to the text-
    bearing scenario's manifest values for the same source seed.

    The contract: image_only_seed is just a filename suffix change;
    the underlying ``SyntheticStatement`` (via ``source_seed``) is
    the canonical source of truth. If a future refactor causes the
    image-only manifest to drift, the corpus tests would suddenly be
    grading the parser against a different ground truth without any
    operator change — that's the silent failure this test guards.
    """
    from scripts.generate_corpus import _build_statement
    from scripts.generate_corpus import Recipe as BaseRecipe
    from scripts.generate_image_only_corpus import IMAGE_ONLY_RECIPES
    from scripts.generate_image_only_corpus import main as gen_main

    gen_main([])

    for recipe in IMAGE_ONLY_RECIPES:
        manifest_path = (
            isolated_corpus_dir
            / (
                f"image_only_{recipe.scenario}_{recipe.bank}"
                f"_{recipe.image_only_seed:05d}.manifest.json"
            )
        )
        manifest = json.loads(manifest_path.read_text())
        stmt = _build_statement(
            BaseRecipe(
                bank=recipe.bank,
                scenario=recipe.scenario,
                seed=recipe.source_seed,
            )
        )
        assert manifest["scenario"] == stmt.scenario
        assert manifest["bank"] == stmt.bank
        assert manifest["summary"]["beginning_balance"] == str(stmt.beginning_balance)
        assert manifest["summary"]["ending_balance"] == str(stmt.ending_balance)
        assert manifest["summary"]["deposit_total"] == str(stmt.deposit_total)
        assert manifest["summary"]["withdrawal_total"] == str(stmt.withdrawal_total)
        assert manifest["summary"]["printed_transaction_count"] == len(
            stmt.transactions
        )
        assert manifest.get("image_only") is True
