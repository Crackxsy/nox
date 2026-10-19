/**
 * The two hooks every page used to hand-roll.
 *
 * `useIpcAction` owns the `busy` / `error` pair that appeared, slightly differently spelled, in six
 * pages: it disables the button that is in flight, keeps exactly one error string, and turns a
 * thrown `IpcError` into a sentence through `errorText`. Because it owns `busy`, a second click on
 * "Speichern" cannot send a second request.
 *
 * `useRefreshOnConnect` is the "run this once per (re)connect" effect the pages wrote by hand with
 * an `eslint-disable` on top. It cancels in-flight work on unmount and on a reconnect, which is the
 * half the hand-written versions kept forgetting.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { errorText, ipcReason } from '../../shared/errors';
import { type T, ipcReasonLabel } from './i18n';
import { type IpcClient, IpcError } from './ipc';
import { pinErrorKey } from './model';

/**
 * A refusal in the words of this UI: the core's machine-readable reason first (`details.reason`),
 * then the PIN wording the local PIN gate implies, and only then `fallback` - or, without one, the
 * core's own message behind the error prefix (honest, but English operator text).
 */
export function refusalText(t: T, e: unknown, pinSent = false, fallback?: string): string {
  const reason = ipcReasonLabel(t, ipcReason(e));
  if (reason) return reason;
  if (e instanceof IpcError) {
    const pin = pinErrorKey(e.code, e.message, pinSent);
    if (pin) return t(pin);
  }
  return fallback ?? errorText(t('error_prefix'), e);
}

export interface IpcAction {
  /** The label passed to `run`, while that call is in flight; `null` when nothing is. */
  busy: string | null;
  /** The last failure, already translated and prefixed; `''` when the last call succeeded. */
  error: string;
  setError: (message: string) => void;
  /** Runs `fn` with the client, guarding it with `busy` and catching into `error`. */
  run: (label: string, fn: (c: IpcClient) => Promise<unknown>) => Promise<void>;
}

/**
 * `describe` turns a failure into the sentence shown; by default the core's message behind the
 * error prefix. Pages that know the core's refusal reasons pass `refusalText`.
 */
export function useIpcAction(
  client: IpcClient | null,
  t: T,
  describe?: (e: unknown) => string,
): IpcAction {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  const busyRef = useRef<string | null>(null);
  // Read through a ref, so an inline `describe` does not make `run` a new function every render.
  const describeRef = useRef(describe);
  useEffect(() => {
    describeRef.current = describe;
  });

  const run = useCallback(
    async (label: string, fn: (c: IpcClient) => Promise<unknown>) => {
      if (!client || busyRef.current !== null) return;
      busyRef.current = label;
      setBusy(label);
      setError('');
      try {
        await fn(client);
      } catch (e) {
        const own = describeRef.current;
        setError(own ? own(e) : errorText(t('error_prefix'), e));
      } finally {
        busyRef.current = null;
        setBusy(null);
      }
    },
    [client, t],
  );

  return { busy, error, setError, run };
}

/**
 * Run `load` once for every client identity (i.e. once per connection), with a cancellation flag
 * the callback can check before it writes state.
 */
export function useRefreshOnConnect(
  client: IpcClient | null,
  load: (c: IpcClient, cancelled: () => boolean) => Promise<void>,
): void {
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  });
  useEffect(() => {
    if (!client) return;
    let cancelled = false;
    void loadRef.current(client, () => cancelled);
    return () => {
      cancelled = true;
    };
  }, [client]);
}

/**
 * Run `tick` every `intervalMs` while the page is visible, and once more whenever it becomes
 * visible again - a list on a phone that was in a pocket is stale the moment it is looked at.
 * Nothing runs while the tab is hidden.
 */
export function useWhileVisible(enabled: boolean, intervalMs: number, tick: () => void): void {
  const tickRef = useRef(tick);
  useEffect(() => {
    tickRef.current = tick;
  });
  useEffect(() => {
    if (!enabled) return;
    let timer = 0;
    const start = () => {
      window.clearInterval(timer);
      timer = window.setInterval(() => tickRef.current(), intervalMs);
    };
    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        tickRef.current();
        start();
      } else {
        window.clearInterval(timer);
      }
    };
    if (document.visibilityState === 'visible') start();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [enabled, intervalMs]);
}
