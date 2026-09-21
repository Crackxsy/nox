/**
 * The "Verbindung testen" button's own state.
 *
 * It is a hook rather than three `useState` calls inside the integrations section for the same
 * reason `useSecrets` is: the button owns a real request, that request has exactly one outcome at
 * a time, and the outcome has to be readable in words — not as a green tick that means "we sent
 * something". The core answers with one of six codes and this hook maps each one onto its own
 * sentence; an unknown code keeps the core's own wording instead of inventing a friendlier one.
 */

import { useCallback, useState } from 'react';

import { useIpcAction } from '../../hooks';
import { type Key, type T, fill } from '../../i18n';
import { type IpcClient, api } from '../../ipc';
import { type HomeTestResult, parseHomeTest } from '../../model';

/** Probe code -> the sentence shown under the button. */
const CODE_KEYS: Record<string, Key> = {
  ok: 'home_test_ok',
  no_token: 'home_test_no_token',
  unauthorized: 'home_test_unauthorized',
  unreachable: 'home_test_unreachable',
  blocked: 'home_test_blocked',
  http_error: 'home_test_http_error',
};

export interface HomeTestController {
  busy: boolean;
  /** The last error from the request itself (not from the probe's verdict). */
  error: string;
  /** The probe's verdict in words, or `''` when it has not run on this page yet. */
  message: string;
  /** The core's own detail line, shown under the sentence; `null` when there is none. */
  detail: string | null;
  ok: boolean | null;
  test: () => void;
}

export function useHomeTest(client: IpcClient | null, t: T): HomeTestController {
  const { busy, error, run } = useIpcAction(client, t);
  const [result, setResult] = useState<HomeTestResult | null>(null);

  const test = useCallback(() => {
    void run('home-test', async (c) => {
      setResult(parseHomeTest(await api.homeTest(c)));
    });
  }, [run]);

  const message = (() => {
    if (result === null) return '';
    const key = CODE_KEYS[result.code];
    if (!key) return result.detail || '';
    return key === 'home_test_ok' ? fill(t(key), result.haVersion) : t(key);
  })();

  return {
    busy: busy === 'home-test',
    error,
    message,
    detail: result && result.code !== 'ok' && result.detail ? result.detail : null,
    ok: result === null ? null : result.ok,
    test,
  };
}
