/**
 * The one line the pet window shows when it cannot talk to the core for a reason the person has to
 * act on: no token at all, a token that was never accepted, or a session that ended because the
 * core restarted.
 *
 * A restart is the common case. The desktop shell reloads this page with the new token as soon as
 * it reconnects, so the note says that first; a browser copy (the OBS overlay, say) cannot learn the
 * new token, and the note says how to reopen Nox instead of retrying something that cannot work.
 * Nothing is shown while simply offline: the capture indicator already says the state is unknown.
 */

import type { ConnStatus } from './ipc';
import type { PetT } from './strings';

export interface ConnectionNoteProps {
  t: PetT;
  hasToken: boolean;
  status: ConnStatus;
  detail?: string;
}

export function ConnectionNote({ t, hasToken, status, detail }: ConnectionNoteProps) {
  if (!hasToken) {
    return (
      <p role="alert" className="pet-chip pet-note">
        {t('no_token')}
      </p>
    );
  }
  if (status === 'session_expired') {
    return (
      <p role="alert" className="pet-chip pet-note">
        {t('session_expired')}
      </p>
    );
  }
  if (status === 'auth_failed') {
    return (
      <p role="alert" className="pet-chip pet-note">
        {t('auth_denied')}
        {detail ? `: ${detail}` : ''}
      </p>
    );
  }
  return null;
}
