/**
 * Which setting lives on which of the eight Settings pages (Spec UX-Neubau §6, §7).
 *
 * Every page lists its rows in three tiers: visible, "Mehr Optionen", and - inside the components -
 * "Technische Angaben". A setting the core reports that no page names here is not dropped: System ›
 * Mehr Optionen lists every leftover path (`unplacedPaths`), so a new setting in the core is still
 * editable before anyone decides where it belongs.
 */

import type { Key } from '../../i18n';
import type { SettingsPageId } from '../../shell/routes';

export interface SettingsSection {
  /** Section heading; omitted for the first, untitled block of a page. */
  title?: Key;
  paths: readonly string[];
}

export interface SettingsPageLayout {
  id: SettingsPageId;
  title: Key;
  /** One sentence under the page heading. */
  lede: Key;
  sections: readonly SettingsSection[];
  more: readonly string[];
}

export const SETTINGS_LAYOUT: readonly SettingsPageLayout[] = [
  {
    id: 'privacy',
    title: 'ux_set_privacy',
    lede: 'ux_set_privacy_lede',
    sections: [
      {
        title: 'ux_set_privacy_perceive',
        paths: ['privacy.capture.microphone', 'privacy.capture.camera', 'privacy.capture.screen'],
      },
      { title: 'ux_set_privacy_control', paths: ['desktop.computer_use.enabled'] },
    ],
    more: ['ipc.lan.enabled', 'remote.enabled'],
  },
  {
    id: 'voice',
    title: 'ux_set_voice',
    lede: 'ux_set_voice_lede',
    sections: [
      {
        title: 'ux_set_voice_sound',
        paths: ['voice.tts.engine', 'voice.tts.voice', 'voice.tts.rate', 'voice.tts.volume'],
      },
      {
        title: 'ux_set_voice_where',
        paths: ['voice.channels.routing', 'voice.channels.private_device', 'voice.channels.stream_device'],
      },
      {
        title: 'ux_set_voice_when',
        paths: ['voice.stt.listening_mode', 'voice.stt.wake_word', 'voice.stt.push_to_talk_hotkey'],
      },
    ],
    more: [
      'voice.stt.model',
      'voice.stt.wake_word_engine',
      'voice.stt.vocabulary',
      'voice.stt.language',
      'voice.tts.steps',
    ],
  },
  {
    id: 'stream',
    title: 'ux_set_stream',
    lede: 'ux_set_stream_lede',
    sections: [{ paths: ['stream.twitch.channel', 'stream.ai_voice_disclosure.enabled'] }],
    more: [
      'stream.ai_voice_disclosure.chat_text',
      'stream.ai_voice_disclosure.overlay_label',
      'stream.twitch.bot_names',
      'stream.twitch.relevance_cooldown_s',
      'stream.twitch.rate_limit_max_messages',
      'stream.twitch.rate_limit_window_s',
      'stream.twitch.rate_limit_min_gap_s',
      'stream.twitch.min_backoff_s',
      'stream.twitch.max_backoff_s',
    ],
  },
  {
    id: 'you',
    title: 'ux_set_you',
    lede: 'ux_set_you_lede',
    sections: [{ paths: ['identity.user_display_name'] }],
    more: [],
  },
  {
    id: 'nox',
    title: 'ux_set_nox',
    lede: 'ux_set_nox_lede',
    sections: [{ paths: ['identity.name', 'pet.variant', 'pet.greeting_enabled'] }],
    more: [],
  },
  {
    id: 'home',
    title: 'ux_set_home',
    lede: 'ux_set_home_lede',
    sections: [{ paths: ['home.host', 'home.port', 'home.tls', 'home.areas_allowed'] }],
    more: [],
  },
  {
    id: 'ai',
    title: 'ux_set_ai',
    lede: 'ux_set_ai_lede',
    sections: [{ paths: ['ai.router.default_reasoner', 'ai.router.fallback_chain'] }],
    more: [],
  },
  {
    id: 'system',
    title: 'ux_set_system',
    lede: 'ux_set_system_lede',
    sections: [{ title: 'ux_set_system_look', paths: ['identity.ui_language', 'identity.speech_language'] }],
    more: ['plugins.enabled', 'memory.full_scan_on_boot'],
  },
];

export function layoutOf(id: SettingsPageId): SettingsPageLayout {
  return SETTINGS_LAYOUT.find((page) => page.id === id) ?? SETTINGS_LAYOUT[0]!;
}

const PLACED: ReadonlySet<string> = new Set(
  SETTINGS_LAYOUT.flatMap((page) => [...page.sections.flatMap((s) => s.paths), ...page.more]),
);

/** Which page a setting is on, for the command palette's "Gehe zu". */
export function pageOfPath(path: string): SettingsPageId {
  for (const page of SETTINGS_LAYOUT) {
    if (page.more.includes(path) || page.sections.some((s) => s.paths.includes(path))) return page.id;
  }
  return 'system';
}

/**
 * Settings the dashboard never shows as a raw field. `security.profile` is set by the Modus (one
 * control for mode and rules, owner decision): a second, separate field for it would let the two
 * disagree.
 */
export const NEVER_SHOWN: ReadonlySet<string> = new Set(['security.profile']);

/** Paths the core reports that no page above names: System › Mehr Optionen shows them. */
export function unplacedPaths(reported: readonly string[]): string[] {
  return reported.filter((path) => !PLACED.has(path) && !NEVER_SHOWN.has(path));
}

/** Every path the layout names, in page order (the palette indexes these). */
export const PLACED_PATHS: readonly string[] = [...PLACED];
