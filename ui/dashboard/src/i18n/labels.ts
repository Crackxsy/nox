/**
 * Identifier → word. Every value the core sends as a machine id (`companion`, `ai.rules`,
 * `marker_promoted`, `allow`, `continuous`) passes through one of the tables below before it
 * reaches a screen, because §7 of the code standards says labels are words, never identifiers.
 *
 * All of them share the same honesty rule: an id this table does not know is shown **verbatim**.
 * Inventing a friendly name for an unknown value would be worse than showing the id — the user
 * could not look it up, and we could not tell that the table had gone stale.
 *
 * `reasonLine` is the same idea for the free-text `reason` strings the core's health checks and
 * providers produce in English. It returns a translated sentence *plus* the original, so the page
 * can print the German line and keep the core's own wording in a muted second line — nothing is
 * hidden, and a reason nobody has translated yet still arrives on screen intact.
 */

import { type Key, type Lang, type T, fill } from './core';
import { de } from './de';

function lookup(t: T, table: Record<string, Key>, value: string): string {
  const key = table[value];
  return key ? t(key) : value;
}

const STATUS_KEYS: Record<string, Key> = {
  available: 'status_available',
  limited: 'status_limited',
  unavailable: 'status_unavailable',
};

/** Health status word. */
export function statusLabel(t: T, status: string): string {
  return lookup(t, STATUS_KEYS, status);
}

const LEVEL_KEYS: Record<string, Key> = {
  running: 'level_running',
  starting: 'level_starting',
  stopping: 'level_stopping',
  safe_mode: 'level_safe_mode',
  degraded: 'level_degraded',
};

/** `system.level`: the word above the home screen's largest tile. */
export function levelLabel(t: T, level: string): string {
  return lookup(t, LEVEL_KEYS, level);
}

const MODE_KEYS: Record<string, Key> = {
  companion: 'mode_companion',
  coding: 'mode_coding',
  project: 'mode_project',
  stream: 'mode_stream',
  rocket_league: 'mode_rocket_league',
  creative: 'mode_creative',
  research: 'mode_research',
  focus: 'mode_focus',
  idle: 'mode_idle',
};

/** `assistant.mode` as a plain noun ("Begleiter"), for a select option. */
export function modeLabel(t: T, mode: string): string {
  return lookup(t, MODE_KEYS, mode);
}

/** The same mode as a headline ("Begleiter-Modus"). */
export function modeHeadline(t: T, mode: string): string {
  return fill(t('mode_suffix'), modeLabel(t, mode));
}

const PROFILE_KEYS: Record<string, Key> = {
  companion: 'profile_companion',
  coding: 'profile_coding',
  stream: 'profile_stream',
  research: 'profile_research',
  work: 'profile_work',
  offline: 'profile_offline',
  rocket_league: 'profile_rocket_league',
};

/** `security.profile` — the profile that gates plugins and egress, not the assistant mode. */
export function profileLabel(t: T, profile: string): string {
  return lookup(t, PROFILE_KEYS, profile);
}

const PRIVACY_KEYS: Record<string, Key> = {
  full: 'privacy_full',
  balanced: 'privacy_balanced',
  private: 'privacy_private',
  offline: 'privacy_offline',
};

export function privacyLabel(t: T, mode: string): string {
  return lookup(t, PRIVACY_KEYS, mode);
}

const COMPONENT_KEYS: Record<string, Key> = {
  'ai.claude_code': 'component_ai_claude_code',
  'ai.ollama': 'component_ai_ollama',
  'ai.rules': 'component_ai_rules',
  db: 'component_db',
  vault: 'component_vault',
  voice: 'component_voice',
  'memory.embeddings': 'component_memory_embeddings',
  pm: 'component_pm',
  sensors: 'component_sensors',
  tokens: 'component_tokens',
};

/** Health component id → the thing a user recognises. */
export function componentLabel(t: T, component: string): string {
  return lookup(t, COMPONENT_KEYS, component);
}

const ROLE_KEYS: Record<string, Key> = {
  chat: 'role_chat',
  classify: 'role_classify',
  background: 'role_background',
  // `nox.ai.base.AiRole` spells these `reason` and `code`; `reasoning` stays as an alias so an
  // older core's wording is still a word rather than an identifier.
  reason: 'role_reason',
  reasoning: 'role_reasoning',
  code: 'role_code',
  embedding: 'role_embedding',
  vision: 'role_vision',
};

export function roleLabel(t: T, role: string): string {
  return lookup(t, ROLE_KEYS, role);
}

export function roleList(t: T, roles: readonly string[]): string {
  return roles.map((r) => roleLabel(t, r)).join(', ');
}

const AUDIT_DECISION_KEYS: Record<string, Key> = {
  allow: 'audit_decision_allow',
  deny: 'audit_decision_deny',
  confirm: 'audit_decision_confirm',
};

const AUDIT_RESULT_KEYS: Record<string, Key> = {
  ok: 'audit_result_ok',
  failed: 'audit_result_failed',
  denied: 'audit_result_denied',
  aborted: 'audit_result_aborted',
};

const AUDIT_ACTOR_KEYS: Record<string, Key> = {
  egress: 'audit_actor_egress',
  core: 'audit_actor_core',
  user: 'audit_actor_user',
  dashboard: 'audit_actor_dashboard',
  shell: 'audit_actor_shell',
};

const AUDIT_TOOL_KEYS: Record<string, Key> = { network: 'audit_tool_network' };
const AUDIT_ACTION_KEYS: Record<string, Key> = { request: 'audit_action_request' };

export function auditDecisionLabel(t: T, value: string): string {
  return lookup(t, AUDIT_DECISION_KEYS, value);
}

export function auditResultLabel(t: T, value: string): string {
  return lookup(t, AUDIT_RESULT_KEYS, value);
}

export function auditActorLabel(t: T, value: string): string {
  return lookup(t, AUDIT_ACTOR_KEYS, value);
}

export function auditToolLabel(t: T, value: string): string {
  return lookup(t, AUDIT_TOOL_KEYS, value);
}

export function auditActionLabel(t: T, value: string): string {
  return lookup(t, AUDIT_ACTION_KEYS, value);
}

const CLIP_SOURCE_KEYS: Record<string, Key> = {
  event: 'clip_source_event',
  manual: 'clip_source_manual',
  marker_promoted: 'clip_source_marker_promoted',
};

const CLIP_TRIGGER_KEYS: Record<string, Key> = {
  manual: 'clip_trigger_manual',
  marker: 'clip_trigger_marker',
  highlight: 'clip_trigger_highlight',
  goal: 'clip_trigger_goal',
  save: 'clip_trigger_save',
};

const CLIP_STATUS_KEYS: Record<string, Key> = {
  new: 'clips_status_new',
  reviewed: 'clips_status_reviewed',
  exported: 'clips_status_exported',
  discarded: 'clips_status_discarded',
};

export function clipSourceLabel(t: T, value: string): string {
  return lookup(t, CLIP_SOURCE_KEYS, value);
}

export function clipTriggerLabel(t: T, value: string): string {
  return lookup(t, CLIP_TRIGGER_KEYS, value);
}

/** An unknown clip status stays verbatim; it is never relabelled "neu" (that invents a state). */
export function clipStatusLabel(t: T, value: string): string {
  return lookup(t, CLIP_STATUS_KEYS, value);
}

const TIER_KEYS: Record<string, Key> = {
  none: 'tier_none',
  bronze: 'tier_bronze',
  silver: 'tier_silver',
  gold: 'tier_gold',
  platinum: 'tier_platinum',
};

export function tierLabel(t: T, value: string): string {
  return lookup(t, TIER_KEYS, value);
}

const TWITCH_STATE_KEYS: Record<string, Key> = {
  idle: 'twitch_state_idle',
  pending: 'twitch_state_pending',
  authorized: 'twitch_state_authorized',
  expired: 'twitch_state_expired',
  error: 'twitch_state_error',
};

export function twitchStateLabel(t: T, state: string): string {
  return lookup(t, TWITCH_STATE_KEYS, state);
}

const PLUGIN_STATUS_KEYS: Record<string, Key> = {
  connected: 'plugin_connected',
  disconnected: 'plugin_disconnected',
  unknown: 'plugin_unknown',
};

/** Stream Bot plugin connection word (`nox.stream.sessions` "obs"/"twitch" status strings). */
export function pluginStatusLabel(t: T, status: string): string {
  return lookup(t, PLUGIN_STATUS_KEYS, status);
}

const GROUP_KEYS: Record<string, Key> = {
  identity: 'settings_group_identity',
  voice: 'settings_group_voice',
  privacy: 'settings_group_privacy',
  ai: 'settings_group_ai',
  pet: 'settings_group_pet',
  integrations: 'settings_group_integrations',
  memory: 'settings_group_memory',
  plugins: 'settings_group_plugins',
  remote: 'settings_group_remote',
  home: 'settings_group_home',
};

/** Settings group heading; an unknown group name is shown verbatim. */
export function groupLabel(t: T, group: string): string {
  return lookup(t, GROUP_KEYS, group);
}

/**
 * Label for one editable config path. The dictionary carries `setting_<path with dots replaced by
 * underscores>`; when it does not, the raw path is shown rather than an invented prose label. The
 * i18n test asserts the table covers every path the core's schema reports, so a new setting fails
 * CI instead of shipping its dotted path to a user.
 */
export function settingLabel(t: T, path: string): string {
  const key = `setting_${path.replace(/\./g, '_')}`;
  return key in de ? t(key as Key) : path;
}

/**
 * Option text for one `enum` setting. Scoped by path first, because the same value means different
 * things in different places (`de` is a language here and could be something else elsewhere).
 */
const OPTION_KEYS: Record<string, Record<string, Key>> = {
  'voice.stt.listening_mode': {
    continuous: 'option_listening_continuous',
    ptt_only: 'option_listening_ptt_only',
  },
  'voice.stt.wake_word_engine': {
    openwakeword: 'option_wake_openwakeword',
    text: 'option_wake_text',
  },
  'voice.tts.engine': { piper: 'option_tts_piper', kokoro: 'option_tts_kokoro' },
  'voice.channels.routing': {
    private: 'option_routing_private',
    stream: 'option_routing_stream',
    both: 'option_routing_both',
    mute: 'option_routing_mute',
  },
  'identity.ui_language': { de: 'option_lang_de', en: 'option_lang_en' },
  'identity.speech_language': {
    de: 'option_lang_de',
    en: 'option_lang_en',
    auto: 'option_lang_auto',
  },
  'security.profile': PROFILE_KEYS,
};

export function optionLabel(t: T, path: string, value: string): string {
  const table = OPTION_KEYS[path];
  return table ? lookup(t, table, value) : value;
}

/** Voice routing outside the settings form (the Status page's "Ton geht an" fact). */
export function routingLabel(t: T, value: string): string {
  return optionLabel(t, 'voice.channels.routing', value);
}

// ---- free-text reasons -------------------------------------------------------------------------

/**
 * One health/plugin `reason`, translated where we know it.
 *
 * `original` is non-null exactly when `text` is a *translation*: the caller then prints the German
 * line and the core's own English below it in a muted secondary line. When nothing matched,
 * `text` is the core's string and `original` is null — one line, verbatim, never dropped.
 */
export interface ReasonLine {
  text: string;
  original: string | null;
}

const REASON_KEYS: Record<string, Key> = {
  ok: 'reason_ok',
  corrupt: 'reason_corrupt',
  missing: 'reason_missing',
  disabled: 'reason_disabled',
  'disabled in config': 'reason_disabled_config',
  'deterministic rules, always available': 'reason_rules_always',
  'worker registered': 'reason_worker_registered',
  'worker starting': 'reason_worker_starting',
  'worker not running': 'reason_worker_not_running',
  'not probed yet': 'reason_not_probed',
  'owner-only': 'reason_token_owner_only',
  'no session token': 'reason_token_missing',
  'session token not written yet': 'reason_token_pending',
  'could not restrict the token file to this account': 'reason_token_open',
};

const MODEL_AVAILABLE = /^model (.+) available$/;
const MODEL_MISSING = /^model (.+) not pulled$/;
const TIMEOUT_AFTER = /^timeout after /;
const ROUND_TRIP = /^(.+); round-trip (\d+) ms$/;
/** A Windows drive path or a POSIX absolute path: the `vault` check reports the folder itself. */
const ABSOLUTE_PATH = /^(?:[a-zA-Z]:[\\/]|\/)/;

export function reasonLine(t: T, lang: Lang, reason: string): ReasonLine {
  const raw = reason.trim();
  if (raw === '') return { text: '', original: null };

  const key = REASON_KEYS[raw];
  if (key) return { text: t(key), original: raw };

  const available = MODEL_AVAILABLE.exec(raw);
  if (available) return { text: fill(t('reason_model_available'), available[1]), original: raw };

  const missing = MODEL_MISSING.exec(raw);
  if (missing) return { text: fill(t('reason_model_missing'), missing[1]), original: raw };

  if (TIMEOUT_AFTER.test(raw)) return { text: t('reason_timeout'), original: raw };

  const trip = ROUND_TRIP.exec(raw);
  if (trip) {
    const seconds = (Number(trip[2]) / 1000).toFixed(1);
    const latency = lang === 'de' ? `${seconds.replace('.', ',')} s` : `${seconds} s`;
    return { text: fill(t('reason_roundtrip'), trip[1], latency), original: raw };
  }

  // The `vault` check answers with the folder it found. A machine path is never a user-facing
  // label, so it becomes "Ordner erreichbar" and the path itself goes into the secondary line.
  if (ABSOLUTE_PATH.test(raw)) return { text: t('reason_path_ok'), original: raw };

  return { text: raw, original: null };
}

/**
 * The "who answered this" half of a chat byline.
 *
 * German names the provider the way the Status page does ("Antwort von Ollama (llama3.2:3b)"),
 * because that is the user-facing language of this product. English keeps `Provider: <id>`: the
 * English surface is the developer-facing one (code standards §7 — plain German for users, plain
 * English for developers), and `tests/e2e/test_dashboard_live.py` reads exactly that pair.
 */
export function providerByline(t: T, lang: Lang, id: string, displayName: string): string {
  return lang === 'de' ? `${t('chat_provider')} ${displayName || id}` : `${t('chat_provider')}: ${id}`;
}

/**
 * Why a stream plugin is not loaded, when the core's reason names a profile.
 * `profile 'companion' not in ['stream']` → "nicht in diesem Profil aktiv".
 */
const PROFILE_BLOCKED = /profile '([^']+)' (?:not in|does not)/;

export function pluginBlockedProfile(reason: string): string | null {
  const match = PROFILE_BLOCKED.exec(reason);
  return match ? match[1] : null;
}
