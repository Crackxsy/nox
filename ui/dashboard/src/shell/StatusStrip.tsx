/**
 * The strip under the header for states that must never scroll out of sight (Spec UX-Neubau §3):
 * Nox is stopped, Nox steers mouse and keyboard (here, or on a paired PC - one line each, with the
 * same Stopp as the header), there is no connection to Nox. It has no close
 * button - each line goes away when the core says the state ended.
 *
 * "Fortsetzen" follows the core's resume rules unchanged: only the machine Nox runs on may resume
 * (a paired device is told where), and after a security-path stop the core wants the PIN - it
 * answers `ok: false` without one, and the strip then asks for it.
 */

import { useRef, useState } from 'react';

import { refusalText } from '../hooks';
import { type Lang, type T, fill } from '../i18n';
import { type IpcClient, api } from '../ipc';
import { type ComputerUseNotice, clockTime } from '../model';
import { showCombo } from '../combo';
import { StopButton } from './stop';

export interface StatusStripProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  device: boolean;
  stopped: boolean;
  /** When the stop happened, if this page saw it; null when only the level is known. */
  stoppedAt: string | null;
  computerUse: ComputerUseNotice | null;
  /** Computer-use sessions on paired PCs (spec C §1.10). */
  nodeComputerUse?: ComputerUseNotice[];
  /** The core is unreachable (not "still connecting"). */
  offline: boolean;
  /** A paired device the core did not let in because network access is off. */
  lanOff: boolean;
  onReconnect: () => void;
  onResumed: () => void;
}

export function StatusStrip({
  t,
  lang,
  client,
  device,
  stopped,
  stoppedAt,
  computerUse,
  nodeComputerUse = [],
  offline,
  lanOff,
  onReconnect,
  onResumed,
}: StatusStripProps) {
  const [pinAsked, setPinAsked] = useState(false);
  const [releasePin, setReleasePin] = useState('');
  const [releaseError, setReleaseError] = useState('');
  const [pin, setPin] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pinRef = useRef<HTMLInputElement | null>(null);

  const resume = async () => {
    if (!client || busy) return;
    setBusy(true);
    setError('');
    const sent = pin.trim();
    try {
      const reply = (await api.resume(client, sent)) as { ok?: unknown } | null;
      if (reply && reply.ok === true) {
        setPinAsked(false);
        setPin('');
        onResumed();
      } else if (reply && reply.ok === false) {
        setPinAsked(true);
        setPin('');
        setError(sent ? t('pin_wrong') : t('ux_resume_pin'));
        window.setTimeout(() => pinRef.current?.focus(), 0);
      } else {
        // Only an explicit yes counts: an answer that says neither is not a resume.
        setError(t('ux_resume_unclear'));
      }
    } catch (e) {
      setError(refusalText(t, e, sent !== ''));
    } finally {
      setBusy(false);
    }
  };

  const lines = [];

  if (lanOff) {
    lines.push(
      <div key="lan" role="alert" className="strip strip--warn">
        <p>{t('device_lan_off')}</p>
        <button type="button" className="btn btn--sm" onClick={onReconnect}>
          {t('device_reconnect')}
        </button>
      </div>,
    );
  } else if (offline) {
    lines.push(
      <div key="offline" role="alert" className="strip strip--warn">
        <p>
          <strong>{t('ux_offline_title')}</strong> {t('ux_offline_body')}
        </p>
      </div>,
    );
  }

  if (stopped) {
    const clock = stoppedAt ? clockTime(stoppedAt, lang) : '';
    const since = clock ? fill(t('ux_stopped_since'), clock) : '';
    lines.push(
      <div key="stopped" id="stopped-strip" tabIndex={-1} role="alert" className="strip strip--danger">
        <p>
          <strong>{t('ux_stopped_title')}</strong> {since} {device ? t('ux_stopped_device') : t('ux_stopped_body')}
        </p>
        {!device && (
          <form
            className="strip-act"
            onSubmit={(e) => {
              e.preventDefault();
              void resume();
            }}
          >
            {pinAsked && (
              <>
                <label htmlFor="resume-pin" className="sr-only">
                  {t('perm_pin')}
                </label>
                <input
                  ref={pinRef}
                  id="resume-pin"
                  className="input input--pin input--sm"
                  type="password"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  placeholder={t('perm_pin')}
                  value={pin}
                  onChange={(e) => setPin(e.target.value)}
                />
              </>
            )}
            <button type="submit" className="btn btn--primary btn--sm" disabled={!client || busy}>
              {busy ? t('ux_resuming') : t('ux_resume')}
            </button>
          </form>
        )}
        {error && (
          <p role="alert" className="strip-err">
            {error}
          </p>
        )}
      </div>,
    );
  }

  if (computerUse?.active) {
    lines.push(
      <div key="cu" role="alert" className="strip strip--attn">
        <p>
          {computerUse.stop
            ? fill(t('computer_use_banner'), showCombo(computerUse.stop, lang))
            : t('computer_use_banner_no_hotkey')}
        </p>
      </div>,
    );
  }

  for (const node of nodeComputerUse) {
    lines.push(
      <div key={`cu-${node.deviceId}`} role="alert" className="strip strip--attn">
        <p>
          {fill(t('computer_use_banner_node'), node.device)}{' '}
          {node.stop ? fill(t('computer_use_banner_node_hotkey'), showCombo(node.stop, lang)) : ''}
        </p>
        {node.saturated && <p className="hint">{t('computer_use_saturated')}</p>}
        <StopButton placement="inline" />
        {!device && (
          // A lock that outlived its PC (it went silent): lifted only here, with the PIN.
          <form
            className="strip-act"
            onSubmit={(e) => {
              e.preventDefault();
              if (!client) return;
              setReleaseError('');
              api
                .nodeComputerRelease(client, releasePin.trim())
                .then(() => setReleasePin(''))
                .catch((err: unknown) => setReleaseError(refusalText(t, err, true, t('computer_use_release_failed'))));
            }}
          >
            <label className="sr-only" htmlFor={`release-pin-${node.deviceId}`}>
              {t('computer_use_release_pin')}
            </label>
            <input
              id={`release-pin-${node.deviceId}`}
              type="password"
              inputMode="numeric"
              autoComplete="one-time-code"
              className="input input--pin"
              placeholder={t('perm_pin')}
              value={releasePin}
              onChange={(e) => setReleasePin(e.target.value)}
            />
            <button type="submit" className="btn btn--sm" disabled={!client || !releasePin.trim()}>
              {t('computer_use_release')}
            </button>
          </form>
        )}
        {releaseError && (
          <p role="alert" className="strip-err">
            {releaseError}
          </p>
        )}
      </div>,
    );
  }

  if (lines.length === 0) return null;
  return <div className="strips">{lines}</div>;
}
