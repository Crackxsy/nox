/**
 * What the header says about Nox, derived from the state the core reported - nothing is guessed.
 *
 * One word for the lamp ("Hört zu", "Gestoppt", ...), the security profile a mode runs under (the
 * owner's "one Modus" decision: the UI shows a single control, the core switches mode and profile
 * together in `mode.set`), and how many things wait for the person.
 */

import type { DashboardState } from '../model';

export type Presence = 'offline' | 'stopped' | 'live' | 'muted' | 'speaking' | 'listening' | 'ready';

export function presenceOf(state: DashboardState, online: boolean): Presence {
  if (!online) return 'offline';
  if (state.systemLevel === 'safe_mode') return 'stopped';
  if (state.stream.session.active) return 'live';
  if (state.muted === true) return 'muted';
  if (state.voice?.speaking) return 'speaking';
  if (state.voice?.listening) return 'listening';
  return 'ready';
}

/**
 * The profile each mode runs under - a mirror of `nox.core.modes.PROFILE_FOR_MODE`, used only to
 * *show* it under "Technische Angaben". The core decides; `mode.set` answers with the profile it
 * actually put in force, and that answer wins over this table.
 */
const PROFILE_FOR_MODE: Record<string, string> = {
  coding: 'coding',
  stream: 'stream',
  research: 'research',
};

export function profileForMode(mode: string): string {
  return PROFILE_FOR_MODE[mode] ?? 'companion';
}

/** The modes most households need, shown first; the rest sit under "Weitere Modi". */
export const PRIMARY_MODES = ['companion', 'stream', 'coding', 'focus', 'idle'] as const;
export const MORE_MODES = ['project', 'creative', 'research', 'rocket_league'] as const;

export interface WaitingCounts {
  prompts: number;
  facts: number;
  skills: number;
  troubles: number;
}

export function waitingTotal(w: WaitingCounts): number {
  return w.prompts + w.facts + w.skills + w.troubles;
}

/** Health components that are down: each one is a line under "Wartet auf dich". */
export function troublesOf(state: DashboardState): string[] {
  return (state.health?.components ?? [])
    .filter((c) => c.status === 'unavailable')
    .map((c) => c.component);
}
