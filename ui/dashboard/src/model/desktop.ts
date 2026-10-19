/**
 * Computer use: the `desktop.computer_use` bus event `{active, reason, stop}`.
 *
 * The core publishes it whenever a session that moves the mouse and presses keys starts or ends
 * (`src/nox/desktop/install.py`). While one is active every page shows a banner that cannot be
 * dismissed and names the hotkey that ends it at once. The payload is narrowed by hand: it is a
 * bus event, not a pydantic model in `src/nox/ipc/protocol.py`.
 */

import { isRecord, str } from '../../../shared/guards';

export interface ComputerUseNotice {
  active: boolean;
  /** Why the session started or ended, in the core's words (may be empty). */
  reason: string;
  /** The stop hotkey as configured, e.g. `ctrl+alt+end`. */
  stop: string;
}

/** A malformed event is `null`: the banner never appears on a payload nobody can read. */
export function parseComputerUse(payload: unknown): ComputerUseNotice | null {
  if (!isRecord(payload) || typeof payload.active !== 'boolean') return null;
  return { active: payload.active, reason: str(payload.reason), stop: str(payload.stop) };
}
