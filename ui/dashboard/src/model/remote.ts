/**
 * Mobile Companion view model (EPIC-17): the paired device list and the one-time pairing code.
 *
 * These two used to be `as unknown as` casts straight into JSX, which is how `paired_at` could
 * reach a table cell as `undefined`. They are parsers now, like every other payload in this app.
 */

import type {
  RemoteDevice as WireRemoteDevice,
  RemotePairCode as WireRemotePairCode,
} from '../../../shared/generated/ipc';
import { isRecord, numOrNull, str, strOrNull } from '../../../shared/guards';
import type { FieldMap } from './wire';

export interface RemoteDevice {
  id: string;
  name: string;
  channel: string;
  pairedAt: string;
  lastSeenAt: string | null;
  revokedAt: string | null;
  revokedReason: string;
}

/** See `model/wire.ts`. */
export const REMOTE_DEVICE_FIELDS: FieldMap<WireRemoteDevice, RemoteDevice> = {
  id: 'id',
  name: 'name',
  channel: 'channel',
  paired_at: 'pairedAt',
  last_seen_at: 'lastSeenAt',
  revoked_at: 'revokedAt',
  revoked_reason: 'revokedReason',
};

export interface RemoteDeviceList {
  devices: RemoteDevice[];
  /** Whether `remote.enabled` is on. `null` only until the first answer arrives. */
  enabled: boolean;
}

/** `remote.devices.list {}` response: `{devices, enabled}`. */
export function parseRemoteDevices(payload: unknown): RemoteDeviceList | null {
  if (!isRecord(payload) || !Array.isArray(payload.devices)) return null;
  const devices: RemoteDevice[] = [];
  for (const entry of payload.devices) {
    if (!isRecord(entry)) continue;
    const id = str(entry.id);
    if (!id) continue;
    devices.push({
      id,
      name: str(entry.name),
      channel: str(entry.channel),
      pairedAt: str(entry.paired_at),
      lastSeenAt: strOrNull(entry.last_seen_at),
      revokedAt: strOrNull(entry.revoked_at),
      revokedReason: str(entry.revoked_reason),
    });
  }
  return { devices, enabled: payload.enabled === true };
}

export interface RemotePairCode {
  pairingId: string;
  code: string;
  expiresAt: string;
  ttlS: number;
}

/** See `model/wire.ts`. */
export const REMOTE_CODE_FIELDS: FieldMap<WireRemotePairCode, RemotePairCode> = {
  pairing_id: 'pairingId',
  code: 'code',
  expires_at: 'expiresAt',
  ttl_s: 'ttlS',
};

/** `remote.pair.start {name}` response. `null` when no usable code came back. */
export function parsePairCode(payload: unknown): RemotePairCode | null {
  if (!isRecord(payload)) return null;
  const code = str(payload.code);
  if (!code) return null;
  return {
    pairingId: str(payload.pairing_id),
    code,
    expiresAt: str(payload.expires_at),
    ttlS: numOrNull(payload.ttl_s) ?? 0,
  };
}
