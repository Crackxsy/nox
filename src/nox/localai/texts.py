"""What `nox ai setup` and the onboarding step print, in German and English.

The flow speaks in codes (`Outcome`, `Reason`, `Question`); this module turns them into sentences.
The dashboard has its own copy of the wording in its i18n files, because it renders the same codes
in the browser.
"""

from __future__ import annotations

from nox.ai.local_models import (
    NO_LLM_ARM_MAX_RAM_GB,
    NO_LLM_BELOW_RAM_GB,
    NO_LLM_PI_MAX_GENERATION,
    Reason,
)
from nox.localai.setup import Outcome, Phase, Question, SetupState

__all__ = [
    "TEXTS",
    "phase_line",
    "progress_line",
    "question_text",
    "render_outcome",
    "render_report",
    "texts",
]

TEXTS: dict[str, dict[str, str]] = {
    "de": {
        "title": "Lokale KI einrichten",
        "ram": "Arbeitsspeicher: {ram:.1f} GB",
        "vram": "Grafikspeicher (NVIDIA): {vram:.1f} GB",
        "vram_none": "Grafikspeicher: kein Messwert (nur NVIDIA-Karten über nvidia-smi)",
        "pi": "Gerät: Raspberry Pi {gen}",
        "reason_vram": "Gewählt nach Grafikspeicher.",
        "reason_ram_no_vram_reading": (
            "Kein Grafikspeicher-Messwert (AMD, Intel, Apple oder kein nvidia-smi) - "
            "gewählt nach Arbeitsspeicher."
        ),
        "reason_ram_vram_too_small": (
            "Der Grafikspeicher reicht für kein Modell der Tabelle - gewählt nach Arbeitsspeicher; "
            "das Modell läuft dann auf dem Prozessor."
        ),
        "reason_no_llm_pi": (
            "Auf einem Raspberry Pi {pi_max} oder älter ist kein lokales Sprachmodell sinnvoll: "
            "eine Antwort würde Minuten dauern. Empfehlung: Claude Code nutzen oder Ollama auf "
            "einem stärkeren Rechner im Netz ansprechen."
        ),
        "reason_no_llm_arm_low_ram": (
            "Ein ARM-Gerät mit höchstens {arm_max:.0f} GB Arbeitsspeicher kann kein brauchbares "
            "lokales Modell halten. Empfehlung: Claude Code oder Ollama auf einem anderen Rechner."
        ),
        "reason_no_llm_low_ram": (
            "Unter {ram_min:.0f} GB Arbeitsspeicher ist kein lokales Modell sinnvoll. "
            "Empfehlung: Claude Code oder Ollama auf einem anderen Rechner."
        ),
        "recommended": "Empfohlenes Modell: {model} (Download etwa {size:.1f} GB)",
        "unmeasured": (
            "  Dieses Modell wurde vom Projekt noch nicht vermessen; die Prüfung entscheidet."
        ),
        "ollama_ok": "Ollama: läuft (Version {version})",
        "ollama_not_running": "Ollama: installiert, aber nicht erreichbar",
        "ollama_missing": "Ollama: nicht gefunden",
        "has_model": "Das Modell ist bereits vorhanden; es wird nichts heruntergeladen.",
        "pull_blocked": (
            "Datenschutzmodus {mode}: Downloads sind aus. Ein bereits vorhandenes Modell kann "
            "trotzdem geprüft werden."
        ),
        "install_how": "So installierst du Ollama:",
        "start_how": (
            "Starte Ollama (die Ollama-App oder `ollama serve`) und führe den Befehl erneut aus."
        ),
        "q_install_ollama": "Ollama jetzt installieren? Nox fragt dafür um Erlaubnis.",
        "q_pull": "{model} jetzt herunterladen (etwa {size:.1f} GB)?",
        "phase_install": "Installiere Ollama ...",
        "phase_pull": "Lade {model} herunter ...",
        "phase_probe": "Prüfe das Modell (eine Antwort, ein Werkzeugaufruf) ...",
        "phase_write": "Trage das Modell in user.yaml ein ...",
        "progress": "  {status} {pct}",
        "out_recommended": "Nur geprüft (--dry-run); nichts wurde verändert.",
        "out_done": (
            "Fertig: {model} ist eingerichtet und in user.yaml eingetragen. Wirksam nach einem "
            "Neustart von Nox."
        ),
        "out_no_llm": "Es wird kein lokales Modell eingerichtet.",
        "out_ollama_missing": "Ollama fehlt; ohne Ollama gibt es kein lokales Modell.",
        "out_ollama_not_running": "Ollama antwortet nicht unter {url}.",
        "out_install_failed": "Die Installation von Ollama hat nicht geklappt: {detail}",
        "out_privacy_blocked": (
            "Nicht heruntergeladen: im Datenschutzmodus {detail} lädt Nox nichts aus dem Netz. "
            "Wechsle auf ausgewogen oder voll und versuche es erneut."
        ),
        "out_declined": "Abgebrochen; nichts wurde heruntergeladen oder verändert.",
        "out_pull_failed": "Der Download ist fehlgeschlagen: {detail}",
        "out_probe_failed": (
            "Das Modell hat die Prüfung nicht bestanden ({detail}). user.yaml bleibt unverändert."
        ),
        "out_config_failed": "user.yaml konnte nicht geschrieben werden: {detail}",
        "out_failed": "Unerwarteter Fehler: {detail}",
    },
    "en": {
        "title": "Set up a local model",
        "ram": "RAM: {ram:.1f} GB",
        "vram": "VRAM (NVIDIA): {vram:.1f} GB",
        "vram_none": "VRAM: no reading (only NVIDIA cards are measured, through nvidia-smi)",
        "pi": "Device: Raspberry Pi {gen}",
        "reason_vram": "Chosen by VRAM.",
        "reason_ram_no_vram_reading": (
            "No VRAM reading (AMD, Intel, Apple or no nvidia-smi) - chosen by RAM."
        ),
        "reason_ram_vram_too_small": (
            "The VRAM is too small for every model in the table - chosen by RAM; the model will "
            "run on the CPU."
        ),
        "reason_no_llm_pi": (
            "A local language model is not worth it on a Raspberry Pi {pi_max} or older: an "
            "answer would take minutes. Use Claude Code, or point Nox at Ollama on a stronger "
            "machine on your network."
        ),
        "reason_no_llm_arm_low_ram": (
            "An ARM device with at most {arm_max:.0f} GB of RAM cannot hold a useful local model. "
            "Use Claude Code, or Ollama on another machine."
        ),
        "reason_no_llm_low_ram": (
            "Below {ram_min:.0f} GB of RAM a local model is not worth it. Use Claude Code, or "
            "Ollama on another machine."
        ),
        "recommended": "Recommended model: {model} (download about {size:.1f} GB)",
        "unmeasured": "  The project has not measured this model yet; the probe decides.",
        "ollama_ok": "Ollama: running (version {version})",
        "ollama_not_running": "Ollama: installed, but not reachable",
        "ollama_missing": "Ollama: not found",
        "has_model": "The model is already there; nothing will be downloaded.",
        "pull_blocked": (
            "Privacy mode {mode}: downloads are off. A model that is already there can still be "
            "probed."
        ),
        "install_how": "How to install Ollama:",
        "start_how": "Start Ollama (the Ollama app or `ollama serve`) and run this command again.",
        "q_install_ollama": "Install Ollama now? Nox will ask for permission.",
        "q_pull": "Download {model} now (about {size:.1f} GB)?",
        "phase_install": "Installing Ollama ...",
        "phase_pull": "Downloading {model} ...",
        "phase_probe": "Probing the model (one answer, one tool call) ...",
        "phase_write": "Writing the model to user.yaml ...",
        "progress": "  {status} {pct}",
        "out_recommended": "Checked only (--dry-run); nothing was changed.",
        "out_done": (
            "Done: {model} is set up and written to user.yaml. It takes effect when Nox restarts."
        ),
        "out_no_llm": "No local model will be set up.",
        "out_ollama_missing": "Ollama is missing; without it there is no local model.",
        "out_ollama_not_running": "Ollama does not answer at {url}.",
        "out_install_failed": "Installing Ollama did not work: {detail}",
        "out_privacy_blocked": (
            "Not downloaded: in privacy mode {detail} Nox downloads nothing. Switch to balanced "
            "or full and try again."
        ),
        "out_declined": "Cancelled; nothing was downloaded or changed.",
        "out_pull_failed": "The download failed: {detail}",
        "out_probe_failed": "The model failed the probe ({detail}). user.yaml was not changed.",
        "out_config_failed": "Could not write user.yaml: {detail}",
        "out_failed": "Unexpected error: {detail}",
    },
}


def texts(language: str | None) -> dict[str, str]:
    return TEXTS.get((language or "de").lower(), TEXTS["de"])


def _reason_text(t: dict[str, str], reason: Reason) -> str:
    return t[f"reason_{reason.value}"].format(
        pi_max=NO_LLM_PI_MAX_GENERATION,
        arm_max=NO_LLM_ARM_MAX_RAM_GB,
        ram_min=NO_LLM_BELOW_RAM_GB,
    )


def render_report(state: SetupState, language: str | None) -> list[str]:
    """Hardware, recommendation and Ollama - what a dry run shows, and every run shows first."""
    t = texts(language)
    lines = [t["title"], ""]
    hw = state.hardware
    if hw is not None:
        lines.append(t["ram"].format(ram=hw.ram_gb))
        lines.append(
            t["vram"].format(vram=hw.vram_gb) if hw.vram_gb is not None else t["vram_none"]
        )
        if hw.pi_generation is not None:
            lines.append(t["pi"].format(gen=hw.pi_generation))
    rec = state.recommendation
    if rec is not None:
        lines.append("")
        if rec.tier is not None:
            lines.append(t["recommended"].format(model=rec.tier.model, size=rec.tier.download_gb))
            if not rec.tier.measured:
                lines.append(t["unmeasured"])
        lines.append(_reason_text(t, rec.reason))
        if rec.tier is None:
            return lines
    status = state.ollama
    if status is not None:
        lines.append("")
        if status.reachable:
            lines.append(t["ollama_ok"].format(version=status.version or "?"))
            if state.model_present:
                lines.append(t["has_model"])
        else:
            lines.append(t["ollama_not_running"] if status.installed else t["ollama_missing"])
            if status.installed:
                lines.append(t["start_how"])
            else:
                lines.append(t["install_how"])
                lines += [f"  {line}" for line in state.instructions]
    if state.pull_blocked and not state.model_present:
        lines.append(t["pull_blocked"].format(mode=state.privacy_mode))
    return lines


def question_text(question: Question, state: SetupState, language: str | None) -> str:
    t = texts(language)
    if question is Question.INSTALL_OLLAMA:
        return t["q_install_ollama"]
    tier = state.recommendation.tier if state.recommendation is not None else None
    return t["q_pull"].format(
        model=state.model or "?", size=tier.download_gb if tier is not None else 0.0
    )


def phase_line(state: SetupState, language: str | None) -> str | None:
    t = texts(language)
    if state.phase is Phase.INSTALL:
        return t["phase_install"]
    if state.phase is Phase.PULL:
        return t["phase_pull"].format(model=state.model or "?")
    if state.phase is Phase.PROBE:
        return t["phase_probe"]
    if state.phase is Phase.WRITE:
        return t["phase_write"]
    return None


def progress_line(state: SetupState, language: str | None) -> str | None:
    pull = state.pull
    if pull is None or not pull.status:
        return None
    fraction = pull.fraction
    pct = (
        f"{fraction * 100:.0f}% ({pull.completed / 1e9:.2f} / {pull.total / 1e9:.2f} GB)"
        if fraction is not None
        else ""
    )
    return texts(language)["progress"].format(status=pull.status, pct=pct).rstrip()


def render_outcome(state: SetupState, language: str | None, *, base_url: str) -> str:
    t = texts(language)
    outcome = state.outcome or Outcome.FAILED
    return t[f"out_{outcome.value}"].format(
        model=state.model_written or state.model or "?",
        detail=state.detail or "-",
        url=base_url,
    )
