/**
 * Pet-role IPC client (IPC Model: role `pet` may only send ipc.*, pet.interact, state.get).
 * Subscribes to pet.*, privacy.*, system.*, tts.*, voice.* plus state.changed (sleep tier) and
 * settings.changed (#24: `config.set pet.variant` should take effect without a page reload).
 *
 * `settings.changed` is *not* in the server's outbound filters (`REDACTED_FROM` / `RESTRICTED_TO`
 * in `src/nox/ipc/server.py`), so the pet role is allowed to receive it as soon as it asks for it;
 * no server change was needed for the subscription itself. Reading the *value* back is a different
 * matter — see `variantFromStateReply`.
 */

import type { Envelope } from '../../shared/envelope';
import { type ConnStatus, IpcClient, resolveWsUrl } from '../../shared/ipc';

export type { Envelope, ConnStatus };
export { IpcClient, resolveWsUrl };

export const PET_PATTERNS = [
  'pet.*',
  'privacy.*',
  'system.*',
  'tts.*',
  'voice.*',
  'state.changed',
  'settings.changed',
];

/** True for a `settings.changed` event whose `paths` mention the pet variant (#24). */
export function petVariantChanged(env: Envelope): boolean {
  if (env.name !== 'settings.changed') return false;
  const paths = env.payload?.paths;
  return Array.isArray(paths) && paths.some((p) => p === 'pet.variant');
}

/**
 * Pull a usable variant id out of a `state.get` reply.
 *
 * Today's core answers the `pet` role with a fixed public snapshot (`assistant`/`privacy`/`system`,
 * see `_h_state_get` in `src/nox/app.py`) and carries no `pet.variant` in its state at all, so this
 * returns null and the page keeps the variant it was loaded with — the shell reloading the pet URL
 * is the fallback. Both reply shapes are handled so that the day the core does expose the value
 * (either as a `pet` subtree in the public snapshot or as a `{path, value}` answer) nothing here
 * has to change.
 */
export function variantFromStateReply(reply: unknown): string | null {
  if (typeof reply !== 'object' || reply === null) return null;
  const obj = reply as Record<string, unknown>;
  if (obj.path === 'pet.variant' && typeof obj.value === 'string' && obj.value) return obj.value;
  const pet = obj.pet;
  if (typeof pet === 'object' && pet !== null) {
    const value = (pet as Record<string, unknown>).variant;
    if (typeof value === 'string' && value) return value;
  }
  return null;
}

export interface PetIpcHandlers {
  onEvent: (env: Envelope) => void;
  onStatus: (status: ConnStatus, detail?: string) => void;
}

export async function createPetClient(token: string, handlers: PetIpcHandlers): Promise<IpcClient> {
  const url = await resolveWsUrl(window.location);
  const client = new IpcClient({
    url,
    token,
    role: 'pet',
    id: `pet:${Math.random().toString(36).slice(2, 8)}`,
    patterns: PET_PATTERNS,
    clientVersion: '0.1.0',
    onEvent: handlers.onEvent,
    onStatus: handlers.onStatus,
  });
  client.connect();
  return client;
}
