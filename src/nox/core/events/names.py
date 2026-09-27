"""The event name catalogue: every dotted name Nox publishes, in one place.

Names are the contract. They are stable once shipped, they appear in the vault Event
Model and in plugin manifests, and a rename is a breaking change - which is why they
live in a file of their own rather than beside the models that carry them."""

from __future__ import annotations


class E:
    SYSTEM_STARTED = "system.started"
    SYSTEM_STOPPING = "system.stopping"
    SYSTEM_HEALTH_CHANGED = "system.health_changed"
    SYSTEM_MODE_CHANGED = "system.mode_changed"
    SYSTEM_HANDLER_FAILED = "system.handler_failed"  # bus: handler raised (event, handler, error)
    STATE_CHANGED = "state.changed"

    VOICE_INPUT_STARTED = "voice.input_started"
    VOICE_INPUT_STOPPED = "voice.input_stopped"
    VOICE_TRANSCRIPT_PARTIAL = "voice.transcript_partial"
    VOICE_TRANSCRIPT_READY = "voice.transcript_ready"
    VOICE_PTT_PRESSED = "voice.ptt_pressed"
    VOICE_PTT_RELEASED = "voice.ptt_released"
    VOICE_MUTED = "voice.muted"
    VOICE_KILL_PHRASE = (
        "voice.kill_phrase"  # local kill phrase heard; core maps it to security.kill
    )

    TTS_STARTED = "tts.started"
    TTS_CHUNK = "tts.chunk"
    TTS_FINISHED = "tts.finished"
    TTS_INTERRUPTED = "tts.interrupted"

    AI_REQUEST_STARTED = "ai.request_started"
    AI_RESPONSE_CHUNK = "ai.response_chunk"
    AI_RESPONSE_READY = "ai.response_ready"
    AI_REQUEST_FAILED = "ai.request_failed"
    AI_PROVIDER_CHANGED = "ai.provider_changed"

    PET_STATE_CHANGED = "pet.state_changed"
    PET_INTERACTION = "pet.interaction"

    PRIVACY_MODE_CHANGED = "privacy.mode_changed"
    PRIVACY_CAPTURE_CHANGED = "privacy.capture_changed"

    SECURITY_KILL_SWITCH = "security.kill_switch"
    SECURITY_PANIC = "security.panic"
    SECURITY_PERMISSION_REQUESTED = "security.permission_requested"
    SECURITY_PERMISSION_DECIDED = "security.permission_decided"
    SECURITY_AUDIT = "security.audit"

    PLUGIN_STARTED = "plugin.started"
    PLUGIN_STOPPED = "plugin.stopped"
    PLUGIN_FAILED = "plugin.failed"

    MEMORY_CREATED = "memory.created"
    MEMORY_DELETED = "memory.deleted"
    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"

    IPC_CLIENT_CONNECTED = "ipc.client_connected"
    IPC_CLIENT_DISCONNECTED = "ipc.client_disconnected"

    HEALTH_REPORT = "health.report"

    # The stream bot. STREAM_STARTED/STREAM_ENDED were reserved before they had a payload
    # model; GAME_EVENT is still reserved and nothing emits it yet.
    STREAM_STARTED = "stream.started"
    STREAM_ENDED = "stream.ended"
    STREAM_MODE_CHANGED = "stream.mode_changed"
    STREAM_PREFLIGHT_RESULT = "stream.preflight_result"
    STREAM_VIEWER_SEEN = "stream.viewer_seen"
    STREAM_FUNKEN_AWARDED = "stream.funken_awarded"
    STREAM_FUNKEN_CHANGED = "stream.funken_changed"  # nox.stream.funken: every ledger change
    STREAM_MINIGAME_STARTED = "stream.minigame_started"
    STREAM_MINIGAME_ENDED = "stream.minigame_ended"

    OBS_CONNECTED = "obs.connected"
    OBS_DISCONNECTED = "obs.disconnected"
    OBS_SCENE_CHANGED = "obs.scene_changed"
    OBS_HEALTH_CHANGED = "obs.health_changed"
    OBS_CRASH_DETECTED = "obs.crash_detected"
    OBS_AUTO_RESTARTED = "obs.auto_restarted"
    # The recording software's own recording toggle, which is a different thing from the
    # stream going live - that is STREAM_STARTED/STREAM_ENDED.
    OBS_RECORDING_CHANGED = "obs.recording_changed"

    TWITCH_CONNECTED = "twitch.connected"
    TWITCH_DISCONNECTED = "twitch.disconnected"
    TWITCH_RESYNCED = "twitch.resynced"
    TWITCH_CHAT_MESSAGE = "twitch.chat_message"
    TWITCH_COMMAND_INVOKED = "twitch.command_invoked"
    TWITCH_EVENT = "twitch.event"
    TWITCH_CHAT_MOOD_CHANGED = "twitch.chat_mood_changed"
    TWITCH_MODERATION_ACTION = "twitch.moderation_action"

    GAME_EVENT = "game.event"

    # The mobile companion. `remote.message` is what the phone plugin emits
    # for every inbound message from the paired phone; the rest is core-side lifecycle. No remote
    # event ever carries transcript, memory or secret content (IPC Model "Outbound filtering": the
    # `remote` role never receives `voice.transcript_*`/`ai.*`/`memory.*`).
    REMOTE_MESSAGE = "remote.message"
    REMOTE_PAIRING_STARTED = "remote.pairing_started"
    REMOTE_PAIRED = "remote.paired"
    REMOTE_REVOKED = "remote.revoked"
    REMOTE_COMMAND = "remote.command"
    REMOTE_NOTIFICATION_SENT = "remote.notification_sent"

    # Rocket League coaching. `game.detected` and `game.ended` are the generic game-lifecycle
    # events any game plugin may use; `rl.*` stays specific to the `rl` plugin.
    GAME_DETECTED = "game.detected"
    GAME_ENDED = "game.ended"
    RL_MATCH_STARTED = "rl.match_started"
    RL_MATCH_ENDED = "rl.match_ended"
    RL_EVENT = "rl.event"
    RL_REPLAY_PARSED = "rl.replay_parsed"
    RL_CALLOUT = "rl.callout"

    # On-screen analysis during a match. The observation-only boundary is unchanged: a
    # read-only pass over frames the plugin's capture pipeline already produces. Nothing
    # here reads game memory or sends input.
    RL_VISION_DETECTIONS = "rl.vision.detections"  # low-rate, confidence-gated (not audited)
    RL_VISION_DISABLED = "rl.vision.disabled"  # budget guard or manual auto/forced disable (P6)
    RL_VISION_ANALYSIS = "rl.vision.analysis"  # post-match rough rotation-position analysis

    # The clip pipeline. A deliberately small event surface: the
    # `clips` plugin never talks to OBS directly (no cross-plugin tool call in the Plugin API), so
    # it only emits CLIP_REQUESTED; the core-side `nox.clips` service calls `obs.replay_buffer.save`
    # through `ToolExecutor` and reports the outcome as CLIP_SAVED/CLIP_FAILED, and CLIP_EXPORTED is
    # emitted by the core `clip.export` tool. Full spec §8 names (clip.captured/created/reviewed/
    # discarded/trim_created/marker_added) are not implemented in this pass - flagged in the report.
    CLIP_REQUESTED = "clip.requested"
    CLIP_SAVED = "clip.saved"
    CLIP_FAILED = "clip.failed"
    CLIP_EXPORTED = "clip.exported"

    # Creative applications. SENSOR_FOREGROUND_CHANGED is the foreground-window change the
    # activity sensor reports; it is defined here because the creative detection needed it
    # first, and everything else reuses this one definition rather than adding a second.
    SENSOR_FOREGROUND_CHANGED = "sensor.foreground_changed"
    CREATIVE_APP_DETECTED = "creative.app_detected"
    CREATIVE_APP_LEFT = "creative.app_left"
    CREATIVE_NOTE_WRITTEN = "creative.note_written"
    # A plugin cannot see privacy zones, so it asks the core-side creative service to decide
    # whether a screenshot may be taken, and to take it.
    CREATIVE_SCREENSHOT_REQUESTED = "creative.screenshot.requested"
    CREATIVE_SCREENSHOT_RESULT = "creative.screenshot.result"

    # Project management. Only the events something actually emits are listed; a name with no
    # producer would be a promise the code does not keep.
    PM_ITEM_CHANGED = "pm.item_changed"
    PM_FOCUS_CHANGED = "pm.focus_changed"

    # The local awareness sensors. PRIVACY_ZONE_CHANGED
    # formalizes the raw string `"privacy.zone_changed"` that `PrivacyService.observe_foreground()`
    # already logs and that `PetService._on_zone` already subscribes to by that same literal -
    # this just gives it a catalog entry and a payload model, it does not rename anything.
    # SENSOR_PROCESS_* is the generic "a configured process name started/ended" signal the
    # `sensors` package emits for any game plugin (e.g. `rl`) to consume instead of duplicating
    # OS process-watching.
    PRIVACY_ZONE_CHANGED = "privacy.zone_changed"
    SENSOR_PROCESS_STARTED = "sensor.process_started"
    SENSOR_PROCESS_ENDED = "sensor.process_ended"

    # The coding assistant (`plugins/coding`). A narrow event surface: the wider set
    # (coding.session.plan_shown/confirmation_requested/repair_attempt/rolled_back/review_ready/
    # merged/paused/resumed) is not implemented here - `coding.session_progress`'s `stage` field
    # covers plan/implement/test/repairing/review/merge, and repair attempts are visible via
    # `coding.session_progress` (stage=repairing) plus `coding.session_failed`'s `repair_attempts`.
    CODING_SESSION_STARTED = "coding.session_started"
    CODING_SESSION_PROGRESS = "coding.session_progress"
    CODING_SESSION_ENDED = "coding.session_ended"
    CODING_SESSION_FAILED = "coding.session_failed"

    # Proactivity: every `nox.proactive.notify()`
    # decision is observable, whether it was delivered or held back - the dashboard toast panel and
    # the telegram remote plugin both consume PROACTIVE_NOTIFICATION; PROACTIVE_SUPPRESSED never
    # fires for URGENT security/data-loss cases (those always get through, per B.13).
    PROACTIVE_NOTIFICATION = "proactive.notification"
    PROACTIVE_SUPPRESSED = "proactive.suppressed"

    # Memory and the vault. MEMORY_CREATED and MEMORY_DELETED were reserved before they had
    # payload models; MEMORY_INDEX_UPDATED is the watcher re-indexing one note.
    MEMORY_INDEX_UPDATED = "memory.index_updated"

    # Settings (`nox.settings`). SETTINGS_CHANGED names the dotted config paths a
    # `config.set` just wrote - paths only, never values: a setting can hold a display name, a
    # channel or a hotkey, and an event payload is the wrong place for any of them. Every UI that
    # caches a setting re-reads it with `config.get` when this arrives.
    SETTINGS_CHANGED = "settings.changed"
    # Twitch OAuth device-code login (`nox.settings.twitch_auth`) moved on: the dashboard's login
    # panel follows this instead of polling `twitch.auth.status`. Carries no token, ever.
    TWITCH_AUTH_CHANGED = "twitch.auth.changed"
