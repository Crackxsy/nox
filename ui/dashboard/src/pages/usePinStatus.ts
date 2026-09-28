/**
 * `security.pin.status`, read whenever it can have changed: on every connection, when the core's
 * system level changes (a kill decides whether resuming needs the PIN), and on demand after this
 * page set or removed the PIN itself.
 *
 * `failed` is kept apart from "no PIN": a page that could not ask must not act as if there were
 * none. The Status page then still offers the PIN field the moment the core asks for one.
 */

import { useCallback, useEffect, useState } from 'react';

import { type IpcClient, api } from '../ipc';
import { type PinState, parsePinStatus } from '../model';

export interface PinStatusView {
  pin: PinState | null;
  /** The last read failed (or answered something unreadable). */
  failed: boolean;
  /** Read again now; resolves when the new state is in. */
  refresh: () => Promise<void>;
}

export function usePinStatus(client: IpcClient | null, refreshKey: unknown = null): PinStatusView {
  const [pin, setPin] = useState<PinState | null>(null);
  const [failed, setFailed] = useState(false);

  const read = useCallback(async (c: IpcClient, cancelled: () => boolean) => {
    try {
      const parsed = parsePinStatus(await api.pinStatus(c));
      if (cancelled()) return;
      setPin(parsed);
      setFailed(parsed === null);
    } catch {
      if (cancelled()) return;
      setPin(null);
      setFailed(true);
    }
  }, []);

  useEffect(() => {
    if (!client) return;
    let cancelled = false;
    void read(client, () => cancelled);
    return () => {
      cancelled = true;
    };
  }, [client, refreshKey, read]);

  const refresh = useCallback(async () => {
    if (client) await read(client, () => false);
  }, [client, read]);

  return { pin, failed, refresh };
}
