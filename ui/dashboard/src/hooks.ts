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

import { errorText } from '../../shared/errors';
import type { T } from './i18n';
import type { IpcClient } from './ipc';

export interface IpcAction {
  /** The label passed to `run`, while that call is in flight; `null` when nothing is. */
  busy: string | null;
  /** The last failure, already translated and prefixed; `''` when the last call succeeded. */
  error: string;
  setError: (message: string) => void;
  /** Runs `fn` with the client, guarding it with `busy` and catching into `error`. */
  run: (label: string, fn: (c: IpcClient) => Promise<unknown>) => Promise<void>;
}

export function useIpcAction(client: IpcClient | null, t: T): IpcAction {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  const busyRef = useRef<string | null>(null);

  const run = useCallback(
    async (label: string, fn: (c: IpcClient) => Promise<unknown>) => {
      if (!client || busyRef.current !== null) return;
      busyRef.current = label;
      setBusy(label);
      setError('');
      try {
        await fn(client);
      } catch (e) {
        setError(errorText(t('error_prefix'), e));
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
