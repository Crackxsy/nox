/**
 * Computer use: the `desktop.computer_use` bus event `{active, reason, stop}`.
 *
 * The core publishes it whenever a session that moves the mouse and presses keys starts or ends
 * (`src/nox/desktop/install.py`) - on this machine, or on a paired PC (`device`, `device_id`, from
 * `src/nox/node/world_routes.py`). While one is active every page shows a banner that cannot be
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
  /** The paired PC being controlled, as the core names it; empty for the machine Nox runs on. */
  device: string;
  /** That PC's node id; empty for the machine Nox runs on. */
  deviceId: string;
  /** That PC reported more addresses than Nox keeps: until the session ends nobody can approve. */
  saturated?: boolean;
}

/** A malformed event is `null`: the banner never appears on a payload nobody can read. */
export function parseComputerUse(payload: unknown): ComputerUseNotice | null {
  if (!isRecord(payload) || typeof payload.active !== 'boolean') return null;
  return {
    active: payload.active,
    reason: str(payload.reason),
    stop: str(payload.stop),
    device: str(payload.device),
    deviceId: str(payload.device_id),
    saturated: payload.saturated === true,
  };
}

/** `node.computer.status {sessions}`: the PCs Nox controls now (malformed entries are dropped). */
export function parseNodeControl(payload: unknown): ComputerUseNotice[] {
  if (!isRecord(payload) || !Array.isArray(payload.sessions)) return [];
  return payload.sessions
    .map(parseComputerUse)
    .filter((n): n is ComputerUseNotice => n !== null && n.active && n.deviceId !== '');
}

/** `node.computer.status {controlled_here}`: this browser runs on a PC Nox controls right now. */
export function parseControlledHere(payload: unknown): boolean {
  return isRecord(payload) && payload.controlled_here === true;
}

/** The sessions on paired PCs after one notice: a start replaces that PC's entry, an end removes it. */
export function applyNodeControl(list: ComputerUseNotice[], notice: ComputerUseNotice): ComputerUseNotice[] {
  const rest = list.filter((n) => n.deviceId !== notice.deviceId);
  return notice.active ? [...rest, notice] : rest;
}
