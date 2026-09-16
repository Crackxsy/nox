"""Rocket League Coach plugin (ST-12-01..08, Spec v0.3 Rocket League Stage 1). Observation only:
game/window process-list detection, HUD-region template matching, the replay watcher/parser, and
the calibration tool. Never sends input, never reads game process memory (Security Model §10) -
`nox.rl.install` owns the core-side services (mode transitions, DB persistence, the callout engine,
session summaries) since a plugin worker has no SQLite/TTS access of its own.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel

from nox.core.events import HealthStatus
from nox.plugins.api import PluginApi
from nox.rl import replay_parser
from nox.rl.capture_gate import CaptureGate
from nox.rl.paths import default_replay_folder
from nox.security.model import Risk

from . import calibration, recognizers
from .detector import GameDetector
from .replay_watcher import ReplayWatcher
from .vision import DEFAULT_ARENA_REGION, FrameSampler, VisionBudgetConfig, build_detector

PARSER_VERSION = replay_parser.PARSER_VERSION


class EmptyInput(BaseModel):
    """Tools that take no arguments."""


class RlPlugin:
    def __init__(self, api: PluginApi) -> None:
        self.api = api
        cfg = api.config
        detection_cfg = dict(cfg.get("detection", {}))
        self.detector = GameDetector(detection_cfg.get("process_names", ["RocketLeague.exe"]))
        self._poll_interval_s = float(detection_cfg.get("poll_interval_s", 2.0))

        folder = cfg.get("replay_folder") or ""
        self._replay_folder = Path(folder) if folder else default_replay_folder()
        self._watcher = ReplayWatcher(self._replay_folder, on_new_file=self._on_new_replay)

        state_path = cfg.get("calibration_state_path") or ""
        self._calibration_state_path = (
            Path(state_path) if state_path else Path.home() / ".nox" / "rl" / "calibration.json"
        )
        self._calibration = calibration.load_calibration(self._calibration_state_path)
        self._templates = recognizers.render_digit_templates()

        self._detect_task: asyncio.Task[None] | None = None
        self._recognize_task: asyncio.Task[None] | None = None
        self._was_running = False
        self._session_started_at = 0.0
        self._match_active = False
        self._match_id_seq = 0
        self._current_match_id: int | None = None
        self._last_scores: tuple[int, int] | None = None
        self._last_boost: int | None = None
        self._replay_index: dict[str, dict[str, Any]] = {}
        self._budget = dict(cfg.get("budget", {}))
        self._capture_fn: Any = calibration.capture_frame

        # -- Vision Stage 2 (ST-18-01..06, Spec v0.9 EPIC-18) -------------------------------------
        vision_cfg = dict(cfg.get("vision", {}))
        self._vision_sample_hz = float(vision_cfg.get("sample_hz", 1.0))
        detector = build_detector(str(vision_cfg.get("backend", "none")))
        self._vision_sampler = FrameSampler(
            detector,
            config=VisionBudgetConfig(
                sample_hz=self._vision_sample_hz,
                max_process_fraction=float(vision_cfg.get("max_process_fraction", 0.5)),
                consecutive_over_budget=int(vision_cfg.get("consecutive_over_budget", 3)),
                cooldown_s=float(vision_cfg.get("cooldown_s", 60.0)),
                min_confidence=float(vision_cfg.get("min_confidence", 0.5)),
            ),
            emit=self._emit_vision_event,
        )
        if not bool(vision_cfg.get("enabled", False)):
            self._vision_sampler.disable_manually()  # opt-in-by-default (Spec §12 open point)
        self._vision_task: asyncio.Task[None] | None = None
        # ST-18-03 AC2 / FR-7.16: both stages stop together when a privacy zone (or a privacy
        # mode, or `privacy.capture.screen: false`) blocks screen capture. One gate for both loops:
        # while it is closed no frame is grabbed at all, and the transition is logged once.
        self._gate = CaptureGate(initially_allowed=True, log=self.api.log)

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        self.api.events.on("system.mode_changed", self._on_mode_changed)
        self.api.events.on("security.kill_switch", self._on_stop_signal)
        self.api.events.on("security.panic", self._on_stop_signal)
        self.api.events.on("privacy.capture_changed", self._on_capture_changed)
        self.api.events.on("privacy.zone_changed", self._on_zone_changed)
        self._watcher.seed_known()
        self._watcher.start()
        self._detect_task = asyncio.create_task(self._detect_loop(), name="rl-detect")

    async def stop(self) -> None:
        for task in (self._detect_task, self._recognize_task, self._vision_task):
            if task is not None:
                task.cancel()
        await self._watcher.stop()

    async def health(self) -> tuple[HealthStatus, str]:
        if self._calibration is None or not self._calibration.calibrated:
            return HealthStatus.LIMITED, "uncalibrated: run `nox rl calibrate` during a match"
        return HealthStatus.AVAILABLE, "calibrated"

    # -- security -------------------------------------------------------------------------------

    async def _on_stop_signal(self, _name: str, _payload: dict[str, Any]) -> None:
        """Kill switch / panic (Spec §6.6): stop callouts/capture within the 2s ack window. This
        plugin never had any effect on the game to begin with, so there is nothing to undo."""
        if self._recognize_task is not None:
            self._recognize_task.cancel()
            self._recognize_task = None
        if self._vision_task is not None:
            self._vision_task.cancel()
            self._vision_task = None

    async def _on_mode_changed(self, _name: str, payload: dict[str, Any]) -> None:
        if str(payload.get("current", "")) != "rocket_league":
            if self._recognize_task is not None:
                self._recognize_task.cancel()
                self._recognize_task = None
            if self._vision_task is not None:
                self._vision_task.cancel()
                self._vision_task = None

    async def _on_capture_changed(self, _name: str, payload: dict[str, Any]) -> None:
        """`privacy.capture_changed` (ST-18-03 AC2): both capture loops go through `_gate`, so
        they pause together within one poll interval and resume together."""
        self._gate.on_capture_changed(payload)

    async def _on_zone_changed(self, _name: str, payload: dict[str, Any]) -> None:
        self._gate.on_zone_changed(payload)

    @property
    def _capture_allowed(self) -> bool:
        return self._gate.allowed

    # -- game detection ------------------------------------------------------------------------

    async def _detect_loop(self) -> None:
        while True:
            running = self.detector.poll()
            if running and not self._was_running:
                self._session_started_at = time.monotonic()
                await self.api.events.emit("game.detected", {"game": "rocket_league"})
                if self._calibration is not None and self._calibration.calibrated:
                    self._recognize_task = asyncio.create_task(
                        self._recognize_loop(), name="rl-recognize"
                    )
                    self._vision_task = asyncio.create_task(self._vision_loop(), name="rl-vision")
            elif not running and self._was_running:
                duration = time.monotonic() - self._session_started_at
                await self._end_match_if_active(reason="normal")
                await self.api.events.emit(
                    "game.ended", {"game": "rocket_league", "duration_s": duration}
                )
                if self._recognize_task is not None:
                    self._recognize_task.cancel()
                    self._recognize_task = None
                if self._vision_task is not None:
                    self._vision_task.cancel()
                    self._vision_task = None
            self._was_running = running
            await asyncio.sleep(self._poll_interval_s)

    # -- HUD recognition (ST-12-05) -------------------------------------------------------------

    async def _recognize_loop(self) -> None:
        """Captures at the configured budget rate, crops the calibrated regions only, derives
        `boost_low`/`goal` events. Overtime/demo banner classification and the live GPU/FPS budget
        guard's automatic reduction are not implemented in this pass - see the ST-12-05 report's
        open points (blocked on ES-04's real HUD screenshots, Plan v0.3)."""
        assert self._calibration is not None
        capture_hz = float(self._budget.get("capture_hz", 2.0))
        interval = 1.0 / capture_hz if capture_hz > 0 else 0.5
        boost_threshold = int(self.api.config.get("callouts", {}).get("boost_low_threshold", 20))
        while True:
            try:
                frame = await self._gate.maybe_capture(lambda: asyncio.to_thread(self._capture_fn))
            except Exception as exc:  # noqa: BLE001 - capture must never crash the plugin
                self.api.log.warning("rl.capture_failed", error=str(exc))
                await asyncio.sleep(interval)
                continue
            if frame is None:  # privacy gate closed: nothing was grabbed, nothing is derived
                await asyncio.sleep(interval)
                continue
            await self._process_frame(frame, boost_threshold=boost_threshold)
            await asyncio.sleep(interval)

    async def _process_frame(self, frame: np.ndarray, *, boost_threshold: int) -> None:
        assert self._calibration is not None
        regions = self._calibration.regions
        if not calibration.has_hud(frame):
            await self._end_match_if_active(reason="normal")
            return
        if not self._match_active:
            await self._start_match()

        boost_crop = calibration.crop_region(frame, regions["boost"])
        boost = recognizers.parse_boost(boost_crop, self._templates)
        if boost is not None:
            value, confidence = boost
            if self._last_boost is not None and self._last_boost > boost_threshold >= value:
                await self.api.events.emit(
                    "rl.event",
                    {
                        "match_id": self._current_match_id,
                        "kind": "boost_low",
                        "source": "hud",
                        "confidence": confidence,
                        "payload": {"boost": value},
                    },
                )
            self._last_boost = value

        self_crop = calibration.crop_region(frame, regions["score_self"])
        opp_crop = calibration.crop_region(frame, regions["score_opponent"])
        self_score = recognizers.parse_score(self_crop, self._templates)
        opp_score = recognizers.parse_score(opp_crop, self._templates)
        if self_score is not None and opp_score is not None:
            scores = (self_score[0], opp_score[0])
            confidence = min(self_score[1], opp_score[1])
            if self._last_scores is not None:
                if scores[0] > self._last_scores[0]:
                    await self.api.events.emit(
                        "rl.event",
                        {
                            "match_id": self._current_match_id,
                            "kind": "goal",
                            "source": "hud",
                            "confidence": confidence,
                            "payload": {
                                "team": "self",
                                "score_self": scores[0],
                                "score_opponent": scores[1],
                            },
                        },
                    )
                elif scores[1] > self._last_scores[1]:
                    await self.api.events.emit(
                        "rl.event",
                        {
                            "match_id": self._current_match_id,
                            "kind": "goal",
                            "source": "hud",
                            "confidence": confidence,
                            "payload": {
                                "team": "opponent",
                                "score_self": scores[0],
                                "score_opponent": scores[1],
                            },
                        },
                    )
            self._last_scores = scores

    async def _start_match(self) -> None:
        self._match_active = True
        self._match_id_seq += 1
        self._current_match_id = self._match_id_seq
        self._last_scores = None
        self._last_boost = None
        await self.api.events.emit(
            "rl.match_started", {"match_id": self._current_match_id, "started_at": _iso_now()}
        )

    async def _end_match_if_active(self, *, reason: str) -> None:
        if not self._match_active:
            return
        self._match_active = False
        scores = self._last_scores or (0, 0)
        result = "unknown"
        if scores[0] != scores[1]:
            result = "win" if scores[0] > scores[1] else "loss"
        label = {"win": "Win", "loss": "Loss"}.get(result, "Match ended")
        summary = f"{label} {scores[0]}-{scores[1]}."
        await self.api.events.emit(
            "rl.match_ended",
            {
                "match_id": self._current_match_id,
                "duration_s": 0.0,
                "result": result,
                "summary_short": summary,
            },
        )
        self._current_match_id = None

    # -- Vision Stage 2 (ST-18-01..06) -----------------------------------------------------------

    async def _vision_loop(self) -> None:
        """Runs alongside `_recognize_loop` (same lifecycle: started/cancelled together - see
        `_detect_loop`/`_on_stop_signal`/`_on_mode_changed`) but at its own, independently
        configured `sample_hz` - a low rate, per Spec §4.2 step 2, never every frame. Reads frames
        from the same injected `_capture_fn` Stage 1 already uses (ST-18-03: no second capture
        mechanism) and is a pure downstream consumer - it never touches HUD region calibration."""
        interval = 1.0 / self._vision_sample_hz if self._vision_sample_hz > 0 else 1.0
        while True:
            if self._vision_sampler.active and self._match_active and self._capture_allowed:
                try:
                    frame = await asyncio.to_thread(self._capture_fn)
                except Exception as exc:  # noqa: BLE001 - capture must never crash the plugin
                    self.api.log.warning("rl.vision.capture_failed", error=str(exc))
                    await asyncio.sleep(interval)
                    continue
                await self._vision_sampler.sample(frame, arena_region=DEFAULT_ARENA_REGION)
            await asyncio.sleep(interval)

    async def _emit_vision_event(self, name: str, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        if name == "rl.vision.detections":
            payload["match_id"] = self._current_match_id
        await self.api.events.emit(name, payload)

    # -- replay watcher (ST-12-02/03) -------------------------------------------------------------

    async def _on_new_replay(self, path: Path) -> None:
        parsed = await asyncio.to_thread(replay_parser.parse_replay_file, str(path))
        summary = parsed.summary() if parsed.parse_status != "failed" else {}
        self._replay_index[path.name] = {
            "file_path": str(path),
            "parse_status": parsed.parse_status,
            "header": summary,
            "errors": parsed.errors,
        }
        await self.api.events.emit(
            "rl.replay_parsed",
            {
                # `replay_id` is assigned by the core-side persistence service on insert; the
                # plugin has no DB access, so it reports the file path and lets core resolve it.
                "replay_id": 0,
                "parse_status": parsed.parse_status,
                "matched_match_id": None,
                "file_path": str(path),
                "header": summary,
                "parser_version": PARSER_VERSION,
            },
        )

    # -- tools ---------------------------------------------------------------------------------

    async def status_read(self, _data: EmptyInput) -> dict[str, Any]:
        """`rl.status.read` (Spec §9/ST-12-08): mode/detection/calibration/budget snapshot."""
        failing_region = None
        if self._calibration is not None and not self._calibration.calibrated:
            failing_region = next(iter(self._calibration.failures), None)
        return {
            "detected": self.detector.running,
            "calibrated": bool(self._calibration and self._calibration.calibrated),
            "failing_region": failing_region,
            "failing_reason": self._calibration.failures.get(failing_region, "")
            if (self._calibration and failing_region)
            else "",
            "active_match_id": self._current_match_id,
            "budget": dict(self._budget),
            "replay_folder": str(self._replay_folder),
            "replays_seen": len(self._replay_index),
        }

    async def replay_list(self, _data: EmptyInput) -> dict[str, Any]:
        """`rl.replay.list`: replays this plugin process has parsed since it started (a durable,
        full index lives in `rl_replays` via the core-side persistence service; this is the
        plugin's own live view, used for the calibration/session panels' immediate feedback)."""
        return {"replays": list(self._replay_index.values())}

    async def replay_summary(self, data: ReplaySummaryInput) -> dict[str, Any]:
        entry = self._replay_index.get(data.file_name)
        if entry is None:
            raise KeyError(f"no parsed replay named {data.file_name!r} in this session")
        return entry

    async def calibrate_tool(self, _data: EmptyInput) -> dict[str, Any]:
        """`rl.calibrate`: dashboard-triggered only (medium risk, confirmed in the `rocket_league`
        profile), never invoked automatically mid-match."""
        result = await asyncio.to_thread(
            calibration.calibrate, state_path=self._calibration_state_path, capture=self._capture_fn
        )
        self._calibration = result
        return {
            "calibrated": result.calibrated,
            "failures": result.failures,
            "regions": result.regions,
        }

    async def vision_status_read(self, _data: EmptyInput) -> dict[str, Any]:
        """`rl.vision.status.read` (ST-18-06): honest state, never "active" while really running
        `NoneDetector` or a budget/manually-disabled sampler (P10)."""
        state, reason = self._vision_sampler.state()
        return {
            "backend": self._vision_sampler.backend_name,
            "state": state.value,
            "reason": reason,
            "active": self._vision_sampler.active,
            "sample_hz": self._vision_sample_hz,
        }

    async def vision_enable_tool(self, data: VisionEnableInput) -> dict[str, Any]:
        """`rl.vision.enable` (medium risk - Spec §4.4 step 4: manual override always available)."""
        if data.enabled:
            self._vision_sampler.enable_manually()
        else:
            self._vision_sampler.disable_manually()
            await self._emit_vision_event(
                "rl.vision.disabled", {"reason": "manually disabled", "automatic": False}
            )
        state, reason = self._vision_sampler.state()
        return {"state": state.value, "reason": reason, "active": self._vision_sampler.active}


class ReplaySummaryInput(BaseModel):
    file_name: str


class VisionEnableInput(BaseModel):
    enabled: bool


def _iso_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def create(api: PluginApi) -> RlPlugin:
    plugin = RlPlugin(api)
    api.tools.register(
        "rl.status.read",
        EmptyInput,
        plugin.status_read,
        Risk.READ,
        description="Current mode/detection/calibration/budget state.",
        side_effects=False,
        local=True,
    )
    api.tools.register(
        "rl.replay.list",
        EmptyInput,
        plugin.replay_list,
        Risk.READ,
        description="Replays parsed by this plugin process.",
        side_effects=False,
        local=True,
    )
    api.tools.register(
        "rl.replay.summary",
        ReplaySummaryInput,
        plugin.replay_summary,
        Risk.READ,
        description="One replay's header/property-tree summary.",
        side_effects=False,
        local=True,
    )
    api.tools.register(
        "rl.calibrate",
        EmptyInput,
        plugin.calibrate_tool,
        Risk.MEDIUM,
        description="Capture one screenshot and (re)calibrate HUD regions.",
        side_effects=True,
        local=True,
    )
    api.tools.register(
        "rl.vision.status.read",
        EmptyInput,
        plugin.vision_status_read,
        Risk.READ,
        description="Vision Stage 2 backend/state/budget snapshot.",
        side_effects=False,
        local=True,
    )
    api.tools.register(
        "rl.vision.enable",
        VisionEnableInput,
        plugin.vision_enable_tool,
        Risk.MEDIUM,
        description="Manually enable/disable Vision Stage 2 (always available, Spec §4.4).",
        side_effects=True,
        local=True,
    )
    return plugin
