"""Sections for the optional feature areas: the stream bot, Rocket League coaching, the clip
pipeline, project management, creative-app detection and the mobile companion.

Numbers marked "starting value" are engineering defaults, not measurements: they were chosen
conservatively so the feature is safe out of the box, and they are meant to be tuned in
`config/user.yaml` once a real installation has data. Changing one never needs a code change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationInfo, field_validator, model_validator

from nox.core.config.types import (
    ExpandedPath,
    OptionalExpandedPath,
    StrictSection,
    require_ordered,
)

__all__ = [
    "CLIP_ROOTS",
    "DEFAULT_CLIPS_ROOT",
    "DEFAULT_CREATIVE_APP_PATTERNS",
    "AppPattern",
    "ClipsConfig",
    "CreativeConfig",
    "FunkenConfig",
    "PmConfig",
    "RelevanceConfig",
    "RemoteConfig",
    "RemoteNotificationsConfig",
    "RlBudgetConfig",
    "RlCalloutConfig",
    "RlConfig",
    "RlDetectionConfig",
    "RlReplayConfig",
    "RlRetentionConfig",
    "RlVisionConfig",
    "StreamChatConfig",
    "StreamConfig",
    "StreamTwitchConfig",
]


# ---- stream bot ---------------------------------------------------------------------------------


class FunkenConfig(StrictSection):
    """Earn rates and loyalty tiers for the channel currency.

    The earn, spend and tier mechanism is fixed; every number below is a starting value behind
    configuration, low enough that a mis-tuned economy cannot be farmed faster than a streamer
    notices.
    """

    earn_per_message: float = Field(default=1.0, ge=0.0)
    earn_per_sub: float = Field(default=50.0, ge=0.0)
    earn_per_bit: float = Field(default=0.1, ge=0.0)
    earn_per_raid: float = Field(default=20.0, ge=0.0)
    earn_cooldown_s: float = Field(default=30.0, gt=0.0)  # anti-farming, message to message
    #: Per-viewer daily earn cap: anti-farming across a whole session rather than just between two
    #: messages. 0 = uncapped.
    earn_daily_cap: float = Field(default=200.0, ge=0.0)
    #: Five loyalty tiers; the thresholds are cumulative balances and must match `tier_names`.
    tier_names: list[str] = Field(
        default_factory=lambda: ["newcomer", "regular", "supporter", "veteran", "legend"]
    )
    tier_thresholds: list[float] = Field(
        default_factory=lambda: [0.0, 100.0, 500.0, 2000.0, 10000.0]
    )

    @field_validator("tier_thresholds")
    @classmethod
    def _same_length_as_names(cls, value: list[float], info: ValidationInfo) -> list[float]:
        names = info.data.get("tier_names")
        if names is not None and len(value) != len(names):
            raise ValueError("tier_thresholds must have exactly one entry per tier_names")
        return value


class RelevanceConfig(StrictSection):
    """How often and how eagerly the bot speaks in chat without being addressed."""

    #: Roughly one unsolicited public message every two minutes.
    unsolicited_interval_s: float = Field(default=120.0, gt=0.0)
    mention_cooldown_s: float = Field(default=5.0, ge=0.0)
    #: The relevance score (0-1) a non-addressed message needs before the responder answers it,
    #: plus the responder's own cadence limits. Separate from `mention_cooldown_s`, which belongs
    #: to the relevance classifier upstream.
    threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    channel_cooldown_s: float = Field(default=20.0, ge=0.0)
    max_replies_per_minute: int = Field(default=6, ge=1)


class StreamChatConfig(StrictSection):
    #: Mirrors `privacy.retention.raw_transcripts_days`: chat is speech, kept just as briefly.
    retain_raw_text_days: int = Field(default=7, ge=0)


class StreamTwitchConfig(StrictSection):
    """What the Twitch bot joins and how loudly it talks - the knobs a streamer actually changes.

    These are configuration rather than plugin-manifest keys, so the dashboard can edit them and
    the plugin reads them from here. The defaults are exactly the values the shipped manifest had,
    so an installation that never touches them behaves as before.
    """

    #: The channel Nox joins; empty = the plugin never joins one. A leading `#` is optional.
    channel: str = ""
    #: Names and aliases the relevance classifier reads as "the bot was addressed".
    bot_names: list[str] = Field(default_factory=lambda: ["nox"])
    #: Per-viewer cooldown before the same viewer's unaddressed chatter counts as relevant again.
    relevance_cooldown_s: float = Field(default=20.0, ge=0.0)
    #: Chat send budget. The default stays under Twitch's own 20 messages per 30 s limit for a
    #: moderator account; a larger value is rate-limited by Twitch instead of by Nox.
    rate_limit_max_messages: int = Field(default=20, ge=1, le=100)
    rate_limit_window_s: float = Field(default=30.0, gt=0.0)
    rate_limit_min_gap_s: float = Field(default=1.5, ge=0.0)
    #: Reconnect backoff, doubled from `min` up to `max` between two connection attempts.
    min_backoff_s: float = Field(default=1.0, gt=0.0)
    max_backoff_s: float = Field(default=30.0, gt=0.0)

    @field_validator("bot_names")
    @classmethod
    def _at_least_one_name(cls, value: list[str]) -> list[str]:
        names = [name.strip() for name in value if name.strip()]
        if not names:
            raise ValueError("bot_names must contain at least one name")
        return names

    @model_validator(mode="after")
    def _backoff_ordered(self) -> StreamTwitchConfig:
        return require_ordered(self, "min_backoff_s", "max_backoff_s")


class StreamConfig(StrictSection):
    funken: FunkenConfig = Field(default_factory=FunkenConfig)
    chat: StreamChatConfig = Field(default_factory=StreamChatConfig)
    relevance: RelevanceConfig = Field(default_factory=RelevanceConfig)
    twitch: StreamTwitchConfig = Field(default_factory=StreamTwitchConfig)


# ---- Rocket League ------------------------------------------------------------------------------


class RlDetectionConfig(StrictSection):
    """Game detection: polling the process list only, never a memory read or an injection."""

    poll_interval_s: float = Field(default=2.0, gt=0.0)
    process_names: list[str] = Field(default_factory=lambda: ["RocketLeague.exe"])


class RlReplayConfig(StrictSection):
    """The replay watcher.

    `folder=None` resolves the possibly cloud-redirected Documents folder at runtime, because its
    literal path differs per machine. Set it explicitly only for a test or a non-default install.
    """

    folder: OptionalExpandedPath = None
    backfill_batch_size: int = Field(default=25, ge=1)
    stable_check_interval_s: float = Field(default=2.0, gt=0.0)
    stable_checks: int = Field(default=2, ge=1)  # consecutive stable size/mtime reads before queue


class RlBudgetConfig(StrictSection):
    """The budget guard for the vision pipeline.

    Capture rate and region shrink automatically before the ceiling is hit, rather than crashing
    or silently exceeding it. The percentages are the ceiling the game must never notice.
    """

    max_gpu_pct: float = Field(default=10.0, gt=0.0, le=100.0)
    max_fps_loss_pct: float = Field(default=5.0, gt=0.0, le=100.0)
    capture_hz: float = Field(default=2.0, gt=0.0)
    min_capture_hz: float = Field(default=0.5, gt=0.0)

    @model_validator(mode="after")
    def _rates_ordered(self) -> RlBudgetConfig:
        return require_ordered(self, "min_capture_hz", "capture_hz")


class RlCalloutConfig(StrictSection):
    """Rate, cooldown and confidence gates for spoken in-game callouts."""

    min_per_minute: int = Field(default=2, ge=0)
    max_per_minute: int = Field(default=4, ge=1)
    cooldown_s: float = Field(default=20.0, ge=0.0)
    boost_low_threshold: int = Field(default=20, ge=0, le=100)
    demo_confidence_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    #: Below this confidence Nox says nothing: silence beats a wrong callout mid-match.
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _rates_ordered(self) -> RlCalloutConfig:
        return require_ordered(self, "min_per_minute", "max_per_minute")


class RlRetentionConfig(StrictSection):
    """Coaching data is only useful over a season, so it outlives a raw transcript by a lot."""

    events_days: int = Field(default=180, ge=0)
    matches_days: int = Field(default=365, ge=0)


class RlVisionConfig(StrictSection):
    """Optional on-screen analysis during a match.

    Off by default even when the Rocket League feature itself is on: the GPU budget on an
    entry-level card is the binding constraint and is not measured, so this never starts
    implicitly. `backend="none"` stays the safe answer even if `enabled` was set without picking a
    backend.
    """

    enabled: bool = False
    backend: Literal["none", "opencv", "onnx"] = "none"
    sample_hz: float = Field(default=1.0, gt=0.0)  # low rate; shares no budget with the callouts
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    #: A cost proxy - measured processing time divided by the sample interval - rather than a live
    #: GPU or frame counter: Nox ships no GPU telemetry dependency, and this fraction is
    #: measurable everywhere.
    max_process_fraction: float = Field(default=0.5, gt=0.0, le=1.0)
    consecutive_over_budget: int = Field(default=3, ge=1)
    cooldown_s: float = Field(default=60.0, ge=0.0)
    #: Detections are not a durable gameplay log: a short window, long enough for the post-match
    #: analysis to still see them.
    detections_retain_hours: float = Field(default=6.0, ge=0.0)


class RlConfig(StrictSection):
    detection: RlDetectionConfig = Field(default_factory=RlDetectionConfig)
    replay: RlReplayConfig = Field(default_factory=RlReplayConfig)
    budget: RlBudgetConfig = Field(default_factory=RlBudgetConfig)
    callouts: RlCalloutConfig = Field(default_factory=RlCalloutConfig)
    retention: RlRetentionConfig = Field(default_factory=RlRetentionConfig)
    vision: RlVisionConfig = Field(default_factory=RlVisionConfig)


# ---- clips --------------------------------------------------------------------------------------

#: Standalone default for the clip roots. `NoxConfig` re-derives them from the real
#: `paths.data_dir` whenever the user has not set them explicitly.
DEFAULT_CLIPS_ROOT = "${APPDATA}/Nox/data/clips"

#: Clip root -> sub-directory of `<paths.data_dir>/data/clips`.
CLIP_ROOTS: dict[str, str] = {
    "library_root": "library",
    "export_root": "export",
    "quarantine_root": "quarantine",
    "watch_dir": "incoming",
}


class ClipsConfig(StrictSection):
    """Clip pipeline roots and triggers.

    The folder the recording software writes to is read-only for Nox: `watch_dir` is only ever
    scanned and copied out of, and `library_root`, `export_root` and `quarantine_root` are the
    only directories the pipeline writes to. Trimming needs `ffmpeg` on the path and reports
    itself unavailable when there is none, rather than assuming a bundled binary.
    """

    library_root: ExpandedPath = Path(f"{DEFAULT_CLIPS_ROOT}/library")
    export_root: ExpandedPath = Path(f"{DEFAULT_CLIPS_ROOT}/export")
    quarantine_root: ExpandedPath = Path(f"{DEFAULT_CLIPS_ROOT}/quarantine")
    #: The replay-buffer output folder of the recording software. Point this at your own.
    watch_dir: ExpandedPath = Path(f"{DEFAULT_CLIPS_ROOT}/incoming")
    #: Game-event kinds that qualify as a highlight.
    highlight_kinds: list[str] = Field(default_factory=lambda: ["goal", "save", "win", "overtime"])
    chat_hype_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    #: Per-trigger-kind cooldown, so one goal cannot produce four clips.
    cooldown_s: float = Field(default=15.0, gt=0.0)
    #: A manual trigger attaches any game event seen in this preceding window as a secondary tag.
    manual_lookback_s: float = Field(default=20.0, ge=0.0)
    watch_poll_interval_s: float = Field(default=2.0, gt=0.0)
    #: How long the watcher waits for a file it does not yet know about before giving up on a clip
    #: request whose own save call did not resolve a path.
    watch_timeout_s: float = Field(default=30.0, gt=0.0)
    #: Discarded clips are moved to the quarantine root and kept this long; never hard-deleted.
    quarantine_days: int = Field(default=30, ge=1)


# ---- project management, creative apps, mobile companion ----------------------------------------


class PmConfig(StrictSection):
    """Vault folders the project-management repository reads and writes, plus watcher tuning."""

    epics_dir: str = "08 - Epics"
    stories_dir: str = "09 - Stories"
    #: A single "projects list note" whose frontmatter carries `projects: [{id, name, status}]`.
    projects_note: str = "Projects.md"
    watch_debounce_s: float = Field(default=1.5, gt=0.0)
    focus_max_items: int = Field(default=5, ge=1)


class AppPattern(StrictSection):
    """One process or window-title rule for `CreativeConfig.app_patterns`.

    Both fields are optional case-insensitive globs; when both are set, both must match. That is
    what a browser-hosted application needs: a browser process *and* a window title.
    """

    process: str | None = None
    title: str | None = None


#: Deliberately narrow: not detecting an application is a much cheaper mistake than announcing a
#: creative session that is not happening. Override the whole map in `config/user.yaml`.
DEFAULT_CREATIVE_APP_PATTERNS: dict[str, list[dict[str, str]]] = {
    "blender": [{"process": "blender.exe"}],
    "fl_studio": [{"process": "fl64.exe"}, {"process": "fl.exe"}],
    "krita": [{"process": "krita.exe"}],
    "capcut": [{"process": "capcut.exe"}],
    "davinci_resolve": [{"process": "resolve.exe"}],
    # A browser-hosted audio workstation: a browser process whose window title matches. Replace
    # the title pattern with the one your own project uses.
    "browser_daw": [
        {"process": "chrome.exe", "title": "*Web DAW*"},
        {"process": "msedge.exe", "title": "*Web DAW*"},
        {"process": "firefox.exe", "title": "*Web DAW*"},
    ],
}


class CreativeConfig(StrictSection):
    """Detecting creative applications, and where their project notes go.

    The `creative` plugin worker reads its own manifest's config block rather than the
    configuration, so the two have to be kept in step by hand until a shared defaults loader
    exists.
    """

    #: How long a match must hold before a mode switch fires, so alt-tabbing does not flip modes.
    hysteresis_s: float = Field(default=15.0, gt=0.0)
    vault_folder: str = "16 - Creative Projects"
    app_patterns: dict[str, list[AppPattern]] = Field(
        default_factory=lambda: {
            family: [AppPattern(**entry) for entry in entries]
            for family, entries in DEFAULT_CREATIVE_APP_PATTERNS.items()
        }
    )


class RemoteNotificationsConfig(StrictSection):
    """Which core events the paired phone may be told about.

    Deliberately an allow-list, not a denylist: an event that is not named here is never
    forwarded. Every entry is still filtered by quiet hours and privacy zones before it leaves the
    machine.
    """

    enabled: bool = True
    events: list[str] = Field(
        default_factory=lambda: [
            "security.kill_switch",
            "system.health_changed",
            "stream.started",
            "stream.ended",
            "pm.focus_changed",
        ]
    )


class RemoteConfig(StrictSection):
    """The mobile companion.

    Off by default: pairing, the remote kill switch and remote privacy switching only exist once
    the user turns this on and pairs a device.
    """

    enabled: bool = False
    #: One-time pairing code lifetime; single use.
    pair_code_ttl_s: float = Field(default=300.0, gt=0.0)
    max_devices: int = Field(default=3, ge=1, le=20)
    #: Per-sender command budget, tighter than any local client: a lost phone is a likelier event
    #: than a compromised local session.
    rate_limit_per_minute: int = Field(default=20, ge=1)
    rate_limit_burst: int = Field(default=5, ge=1)
    #: Chat from the phone goes through the normal orchestrator (same personality, never spoken).
    chat_enabled: bool = True
    notifications: RemoteNotificationsConfig = Field(default_factory=RemoteNotificationsConfig)
