/**
 * What a device sees before the dashboard: pairing it, or being told it is not (or no longer)
 * paired and where a code comes from. One centred card, one decision, plain words - this is the
 * first thing a family member sees after scanning a QR code.
 *
 * The name field starts empty, with this device's own guess ("iPhone") as its placeholder: left
 * empty, the core uses the name given when the code was made, else that guess. After a failed
 * attempt the code field is always shown - also when the code came from the link - so a new code
 * can be typed without going back to the camera.
 *
 * On iOS in a Safari tab, a note says to add Nox to the home screen first and pair inside the app:
 * the app keeps storage of its own, so a pairing made in Safari would not carry over.
 */

import { useState } from 'react';

import {
  type DeviceCredentials,
  type PairError,
  guessDeviceName,
  normaliseCode,
  redeemPairCode,
  saveDevice,
} from './auth';
import type { Key, T } from './i18n';
import { type PlatformInput, currentPlatform, isIosBrowserTab } from './platform';

const ERROR_KEYS: Record<PairError, Key> = {
  unknown_code: 'pair_error_unknown_code',
  used: 'pair_error_used',
  expired: 'pair_error_expired',
  max_devices: 'pair_error_max_devices',
  rate_limited: 'pair_error_rate_limited',
  origin: 'pair_error_origin',
  invalid: 'pair_error_invalid',
  network: 'pair_error_network',
};

export interface PairScreenProps {
  t: T;
  /** From the QR code's link; null when the person types it. */
  code: string | null;
  onPaired: (creds: DeviceCredentials, stored: boolean) => void;
  fetchImpl?: typeof fetch;
  /** This device's own name guess; read from the browser when not given (tests pass one). */
  defaultName?: string;
  /** Read from the browser when not given (tests pass one). */
  platform?: PlatformInput;
}

export function PairScreen({ t, code: initialCode, onPaired, fetchImpl, defaultName, platform }: PairScreenProps) {
  const [guess] = useState(
    () => defaultName ?? guessDeviceName(navigator.userAgent, navigator.maxTouchPoints ?? 0),
  );
  const [iosTab] = useState(() => isIosBrowserTab(platform ?? currentPlatform()));
  const [name, setName] = useState('');
  const [code, setCode] = useState(initialCode ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<PairError | null>(null);
  const valid = normaliseCode(code);
  const showCode = !initialCode || error !== null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    const outcome = await redeemPairCode(valid, name, guess, fetchImpl);
    setBusy(false);
    if (outcome.ok) onPaired(outcome.creds, saveDevice(outcome.creds));
    else setError(outcome.error);
  };

  return (
    <main className="gate">
      <form className="gate-card" onSubmit={(e) => void submit(e)} aria-labelledby="gate-title">
        <p className="wordmark gate-mark" aria-hidden="true">
          Nox
        </p>
        <h1 id="gate-title" className="gate-title">
          {t('pair_title')}
        </h1>
        <p className="gate-lede">{t('pair_lede')}</p>
        {iosTab && (
          <div className="gate-note" role="note" aria-labelledby="pair-ios-title">
            <p id="pair-ios-title" className="gate-note-title">
              {t('pair_ios_title')}
            </p>
            <p>{t('pair_ios_body')}</p>
          </div>
        )}
        <div className="field">
          <label htmlFor="pair-name" className="label">
            {t('pair_name')}
          </label>
          <input
            id="pair-name"
            className="input"
            value={name}
            placeholder={guess || undefined}
            onChange={(e) => setName(e.target.value)}
            maxLength={64}
            autoComplete="off"
            aria-describedby="pair-name-hint"
          />
          <p id="pair-name-hint" className="hint">
            {t('pair_name_hint')}
          </p>
        </div>
        {showCode && (
          <div className="field">
            <label htmlFor="pair-code" className="label">
              {t('pair_code')}
            </label>
            <input
              id="pair-code"
              className="input input--code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoCapitalize="characters"
              autoComplete="one-time-code"
              spellCheck={false}
              maxLength={12}
              placeholder="ABCD EFGH"
              aria-invalid={error === 'unknown_code' || error === 'used' || error === 'expired' || undefined}
              aria-describedby={error ? 'pair-error' : undefined}
            />
          </div>
        )}
        {error && (
          <p id="pair-error" role="alert" className="alert">
            {t(ERROR_KEYS[error])}
          </p>
        )}
        <button type="submit" className="btn gate-button" disabled={!valid || busy}>
          {busy ? t('pair_busy') : t('pair_button')}
        </button>
      </form>
    </main>
  );
}

export type UnpairedReason = 'never' | 'removed' | 'signed_out';

export interface UnpairedScreenProps {
  t: T;
  reason: UnpairedReason;
  onHaveCode: () => void;
}

export function UnpairedScreen({ t, reason, onHaveCode }: UnpairedScreenProps) {
  const lede =
    reason === 'removed'
      ? t('unpaired_removed')
      : reason === 'signed_out'
        ? t('unpaired_signed_out')
        : t('unpaired_body');
  return (
    <main className="gate">
      <section className="gate-card" aria-labelledby="gate-title">
        <p className="wordmark gate-mark" aria-hidden="true">
          Nox
        </p>
        <h1 id="gate-title" className="gate-title">
          {t('unpaired_title')}
        </h1>
        <p className="gate-lede" role={reason === 'never' ? undefined : 'status'}>
          {lede}
        </p>
        {reason !== 'never' && <p className="gate-lede">{t('unpaired_body')}</p>}
        <button type="button" className="btn btn--quiet gate-button" onClick={onHaveCode}>
          {t('unpaired_have_code')}
        </button>
      </section>
    </main>
  );
}
