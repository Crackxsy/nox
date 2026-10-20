/**
 * How a permission question this dashboard showed ended, in one sentence (Spec UX-Neubau §5):
 * "Erlaubt.", "Abgelehnt.", nobody answered in time, decided on another device, cancelled by Stopp.
 * Kept apart from the question itself so the live region is in the page from the start, while the
 * question loads only when one is asked.
 */

import type { Key, T } from './i18n';
import type { DecidedNote } from './model';

const NOTE_KEYS: Record<DecidedNote | 'allowed' | 'denied' | 'stopped', Key> = {
  timeout: 'perm_timed_out',
  elsewhere: 'perm_decided_elsewhere',
  allowed: 'ux_perm_allowed',
  denied: 'ux_perm_denied',
  stopped: 'ux_perm_stopped',
};

export type NoteKind = DecidedNote | 'allowed' | 'denied' | 'stopped';

/**
 * How a question this dashboard showed ended. The live region is always in the page, so the
 * sentence is announced when it appears; it is empty and invisible otherwise.
 */
export function PermissionNote({ t, note }: { t: T; note: NoteKind | null }) {
  const text = note ? t(NOTE_KEYS[note]) : '';
  return (
    <p role="status" className={text ? 'perm-note' : 'perm-note perm-note--empty'}>
      {text}
    </p>
  );
}
