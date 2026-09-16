/**
 * Pet-role IPC client (IPC Model: role `pet` may only send ipc.*, pet.interact, state.get).
 * Subscribes to pet.*, privacy.*, system.*, tts.*, voice.* plus state.changed (sleep tier).
 */

import type { Envelope } from '../../shared/envelope';
import { type ConnStatus, IpcClient, resolveWsUrl } from '../../shared/ipc';

export type { Envelope, ConnStatus };
export { IpcClient, resolveWsUrl };

export const PET_PATTERNS = ['pet.*', 'privacy.*', 'system.*', 'tts.*', 'voice.*', 'state.changed'];

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
