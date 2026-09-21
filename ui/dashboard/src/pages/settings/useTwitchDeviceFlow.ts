/**
 * The Twitch device-code flow, as a state machine with exactly one request in flight.
 *
 * The previous version polled on a `setInterval` whose period was shorter than the request timeout,
 * so `twitch.auth.status` calls overlapped and queued. This one chains: the next poll is scheduled
 * only when the previous one has answered. It also honours `expires_in`, which was parsed and then
 * never used — a device code that expired used to be polled forever.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { errorText } from '../../../../shared/errors';
import type { T } from '../../i18n';
import { type IpcClient, api } from '../../ipc';
import {
  type TwitchAuthStatus,
  type TwitchDeviceCode,
  parseTwitchAuthStatus,
  parseTwitchDeviceCode,
} from '../../model';

export interface TwitchFlow {
  status: TwitchAuthStatus | null;
  failed: boolean;
  device: TwitchDeviceCode | null;
  /** True once the code's `expires_in` has run out without an authorisation. */
  expired: boolean;
  busy: boolean;
  error: string;
  connect: () => void;
  disconnect: () => void;
  /** Re-read `twitch.auth.status` (used when a `twitch.auth.changed` event arrives). */
  refresh: () => void;
}

export function useTwitchDeviceFlow(
  client: IpcClient | null,
  t: T,
  authEvent: string | null,
): TwitchFlow {
  const [status, setStatus] = useState<TwitchAuthStatus | null>(null);
  const [failed, setFailed] = useState(false);
  const [device, setDevice] = useState<TwitchDeviceCode | null>(null);
  const [expired, setExpired] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const readStatus = useCallback(async (c: IpcClient) => {
    try {
      const parsed = parseTwitchAuthStatus(await api.twitchAuthStatus(c));
      setStatus(parsed);
      setFailed(parsed === null);
      return parsed;
    } catch {
      setFailed(true);
      return null;
    }
  }, []);

  useEffect(() => {
    if (client) void readStatus(client);
  }, [client, readStatus]);

  useEffect(() => {
    if (client && authEvent) void readStatus(client);
  }, [client, authEvent, readStatus]);

  // One poll at a time, stopping at `expires_in` or as soon as the state settles.
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!client || !device) return;
    let cancelled = false;
    const deadline = Date.now() + device.expiresIn * 1000;

    const poll = () => {
      void readStatus(client).then((next) => {
        if (cancelled) return;
        if (next && next.state !== 'pending') {
          setDevice(null);
          return;
        }
        if (device.expiresIn > 0 && Date.now() >= deadline) {
          setDevice(null);
          setExpired(true);
          return;
        }
        timerRef.current = setTimeout(poll, device.interval * 1000);
      });
    };

    timerRef.current = setTimeout(poll, device.interval * 1000);
    return () => {
      cancelled = true;
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = null;
    };
  }, [client, device, readStatus]);

  const connect = useCallback(() => {
    if (!client || busy) return;
    setBusy(true);
    setError('');
    setExpired(false);
    void api
      .twitchAuthStart(client)
      .then(async (payload) => {
        const parsed = parseTwitchDeviceCode(payload);
        setDevice(parsed);
        if (parsed) await readStatus(client);
      })
      .catch((e: unknown) => setError(errorText(t('error_prefix'), e)))
      .finally(() => setBusy(false));
  }, [client, busy, readStatus, t]);

  const disconnect = useCallback(() => {
    if (!client || busy) return;
    setBusy(true);
    setError('');
    void api
      .twitchAuthDisconnect(client)
      .then(async () => {
        setDevice(null);
        setExpired(false);
        await readStatus(client);
      })
      .catch((e: unknown) => setError(errorText(t('error_prefix'), e)))
      .finally(() => setBusy(false));
  }, [client, busy, readStatus, t]);

  const refresh = useCallback(() => {
    if (client) void readStatus(client);
  }, [client, readStatus]);

  return { status, failed, device, expired, busy, error, connect, disconnect, refresh };
}
