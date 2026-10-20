"""The curated local-model table: its selection rules, and that the document says the same."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nox.ai.local_models import (
    NO_LLM_ARM_MAX_RAM_GB,
    NO_LLM_BELOW_RAM_GB,
    NO_LLM_PI_MAX_GENERATION,
    TIERS,
    Basis,
    Hardware,
    ModelTier,
    Reason,
    recommend,
)

DOC = Path(__file__).resolve().parents[3] / "docs" / "LOCAL_MODELS.md"


def _doc_section() -> str:
    text = DOC.read_text(encoding="utf-8")
    match = re.search(r"<!-- local-models:begin -->(.*?)<!-- local-models:end -->", text, re.S)
    assert match, "docs/LOCAL_MODELS.md lost its machine-readable section"
    return match.group(1)


def _rows(section: str) -> list[list[str]]:
    rows = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= {"|", "-", " "}:
            continue
        rows.append([cell.strip().strip("`") for cell in line.strip("|").split("|")])
    return rows


def test_the_document_and_the_module_hold_the_same_tiers() -> None:
    rows = _rows(_doc_section())
    tier_rows = [r for r in rows if r[0] in {"vram", "ram"}]
    documented = [
        ModelTier(
            basis=Basis(r[0]),
            min_gb=float(r[1]),
            model=r[2],
            download_gb=float(r[3]),
            measured={"yes": True, "no": False}[r[4]],
        )
        for r in tier_rows
    ]
    assert documented == list(TIERS)


def test_the_document_and_the_module_hold_the_same_no_llm_rules() -> None:
    rules = {r[0]: float(r[1]) for r in _rows(_doc_section()) if len(r) == 2 and r[0] != "Rule"}
    assert rules == {
        "No local model below this much RAM (GB)": NO_LLM_BELOW_RAM_GB,
        "No local model on ARM with at most this much RAM (GB)": NO_LLM_ARM_MAX_RAM_GB,
        "No local model on a Raspberry Pi of this generation or older": float(
            NO_LLM_PI_MAX_GENERATION
        ),
    }


def test_a_6_gb_nvidia_card_gets_the_measured_default() -> None:
    rec = recommend(Hardware(ram_gb=32, vram_gb=6, machine="AMD64"))
    assert rec.model == "qwen3:4b-instruct"
    assert rec.reason is Reason.VRAM


def test_a_large_card_gets_the_larger_tier() -> None:
    rec = recommend(Hardware(ram_gb=64, vram_gb=24, machine="AMD64"))
    assert rec.model == "qwen2.5:14b"
    assert rec.reason is Reason.VRAM


def test_no_vram_reading_chooses_by_ram_and_says_so() -> None:
    rec = recommend(Hardware(ram_gb=16, vram_gb=None, machine="x86_64"))
    assert rec.tier is not None and rec.tier.basis is Basis.RAM
    assert rec.reason is Reason.RAM_NO_VRAM_READING


def test_a_card_too_small_for_any_vram_tier_falls_back_to_ram_and_says_so() -> None:
    rec = recommend(Hardware(ram_gb=8, vram_gb=2, machine="AMD64"))
    assert rec.tier is not None and rec.tier.basis is Basis.RAM
    assert rec.reason is Reason.RAM_VRAM_TOO_SMALL


@pytest.mark.parametrize("ram", [8.0, 4.0])
def test_a_raspberry_pi_4_gets_no_model(ram: float) -> None:
    rec = recommend(Hardware(ram_gb=ram, vram_gb=None, machine="aarch64", pi_generation=4))
    assert rec.tier is None
    assert rec.model is None
    assert rec.reason is Reason.NO_LLM_PI


def test_a_small_arm_board_gets_no_model() -> None:
    rec = recommend(Hardware(ram_gb=4, vram_gb=None, machine="aarch64"))
    assert rec.tier is None
    assert rec.reason is Reason.NO_LLM_ARM_LOW_RAM


def test_low_ram_gets_no_model_even_with_a_card() -> None:
    rec = recommend(Hardware(ram_gb=5.5, vram_gb=8, machine="AMD64"))
    assert rec.tier is None
    assert rec.reason is Reason.NO_LLM_LOW_RAM


def test_every_tier_is_ordered_largest_first_within_its_basis() -> None:
    for basis in Basis:
        sizes = [t.min_gb for t in TIERS if t.basis is basis]
        assert sizes == sorted(sizes, reverse=True)
