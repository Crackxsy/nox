"""Which local model fits this machine: the curated table behind `nox ai setup`.

The same table is printed, in a marked machine-readable section, in `docs/LOCAL_MODELS.md`, and a
unit test keeps the two in agreement - the document is what a person reads before trusting the
recommendation, so it must not drift from what the code actually picks.

Selection is deliberately boring:

1. A machine that cannot run a useful model gets no model, and is told so. That is a Raspberry Pi 4
   or older, an ARM board with at most `NO_LLM_ARM_MAX_RAM_GB` of RAM, and anything with less than
   `NO_LLM_BELOW_RAM_GB` of RAM. A model that answers in a minute is not an assistant.
2. With a VRAM reading (only NVIDIA cards report one, through `nvidia-smi`), the largest VRAM tier
   that fits wins.
3. Otherwise - no reading, or a card too small for any VRAM tier - the RAM tiers decide, and the
   recommendation says that it chose by RAM.

Every entry is an *instruct* model with native tool calling. Bigger is not the win (see the
document): a reasoning model that fits answers ten times slower than an instruct model of half the
size. `measured` says whether the project ran `scripts/probe_models.py` against that model on real
hardware; an unmeasured entry is a reasoned guess, and the probe after the download decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "NO_LLM_ARM_MAX_RAM_GB",
    "NO_LLM_BELOW_RAM_GB",
    "NO_LLM_PI_MAX_GENERATION",
    "TIERS",
    "Basis",
    "Hardware",
    "ModelTier",
    "Reason",
    "Recommendation",
    "recommend",
]

#: Below this much RAM no local model is recommended at all.
NO_LLM_BELOW_RAM_GB = 6.0
#: An ARM board with this much RAM or less gets no local model either.
NO_LLM_ARM_MAX_RAM_GB = 4.0
#: A Raspberry Pi of this generation or older gets no local model, whatever its RAM.
NO_LLM_PI_MAX_GENERATION = 4


class Basis(StrEnum):
    """What a tier is measured against."""

    VRAM = "vram"
    RAM = "ram"


@dataclass(frozen=True, slots=True)
class ModelTier:
    """One row of the table: at least `min_gb` of `basis` -> `model`."""

    basis: Basis
    min_gb: float
    model: str
    #: Approximate size of the default (Q4) download, for an honest "this downloads N GB".
    download_gb: float
    #: Whether the project measured this model with `scripts/probe_models.py`.
    measured: bool


#: Ordered largest first within each basis; `recommend` takes the first that fits.
TIERS: tuple[ModelTier, ...] = (
    ModelTier(Basis.VRAM, 12.0, "qwen2.5:14b", 9.0, measured=False),
    ModelTier(Basis.VRAM, 4.0, "qwen3:4b-instruct", 2.5, measured=True),
    ModelTier(Basis.RAM, 8.0, "qwen3:4b-instruct", 2.5, measured=True),
    ModelTier(Basis.RAM, 6.0, "qwen2.5:3b", 1.9, measured=False),
)


class Reason(StrEnum):
    """Why the recommendation is what it is. Stable codes: the CLI and the dashboard translate
    them, so a reason is never a sentence in only one language."""

    VRAM = "vram"
    #: No VRAM reading at all (no NVIDIA card, or `nvidia-smi` missing): chose by RAM.
    RAM_NO_VRAM_READING = "ram_no_vram_reading"
    #: A VRAM reading too small for every VRAM tier: chose by RAM.
    RAM_VRAM_TOO_SMALL = "ram_vram_too_small"
    NO_LLM_PI = "no_llm_pi"
    NO_LLM_ARM_LOW_RAM = "no_llm_arm_low_ram"
    NO_LLM_LOW_RAM = "no_llm_low_ram"


@dataclass(frozen=True, slots=True)
class Hardware:
    """What the machine has, as far as it can be measured honestly."""

    ram_gb: float
    #: Total VRAM of the first NVIDIA card; None when there is no reading (any other vendor).
    vram_gb: float | None
    #: `platform.machine()`, e.g. `AMD64`, `x86_64`, `aarch64`, `armv7l`.
    machine: str
    #: Raspberry Pi generation (3, 4, 5, ...; 0 for a Zero) or None when this is not a Pi.
    pi_generation: int | None = None

    @property
    def is_arm(self) -> bool:
        return self.machine.lower().startswith(("arm", "aarch"))


@dataclass(frozen=True, slots=True)
class Recommendation:
    """The table's answer for one machine. `tier` is None when no local model is recommended."""

    tier: ModelTier | None
    reason: Reason

    @property
    def model(self) -> str | None:
        return self.tier.model if self.tier is not None else None


def _first_fit(basis: Basis, amount_gb: float) -> ModelTier | None:
    for tier in TIERS:
        if tier.basis is basis and amount_gb >= tier.min_gb:
            return tier
    return None


def recommend(hardware: Hardware) -> Recommendation:
    """The model to set up on `hardware`, or an honest "none" with the reason."""
    if hardware.pi_generation is not None and hardware.pi_generation <= NO_LLM_PI_MAX_GENERATION:
        return Recommendation(None, Reason.NO_LLM_PI)
    if hardware.is_arm and hardware.ram_gb <= NO_LLM_ARM_MAX_RAM_GB:
        return Recommendation(None, Reason.NO_LLM_ARM_LOW_RAM)
    if hardware.ram_gb < NO_LLM_BELOW_RAM_GB:
        return Recommendation(None, Reason.NO_LLM_LOW_RAM)
    if hardware.vram_gb is not None:
        tier = _first_fit(Basis.VRAM, hardware.vram_gb)
        if tier is not None:
            return Recommendation(tier, Reason.VRAM)
    reason = Reason.RAM_NO_VRAM_READING if hardware.vram_gb is None else Reason.RAM_VRAM_TOO_SMALL
    tier = _first_fit(Basis.RAM, hardware.ram_gb)
    if tier is None:  # unreachable while the lowest RAM tier sits at NO_LLM_BELOW_RAM_GB
        return Recommendation(None, Reason.NO_LLM_LOW_RAM)
    return Recommendation(tier, reason)
