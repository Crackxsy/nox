/**
 * Einstellungen › System › Fernwartung (SSH) - only on Nox-OS, the Raspberry Pi image (Spec §2.2).
 *
 * SSH is off there. Two steps, each with the security PIN (always, also on a paired device):
 *
 *   1. **Keys.** Paste public keys (ed25519 or ECDSA). The Pi's root helper stores them - not Nox -
 *      and answers with their fingerprints, which this section shows.
 *   2. **On.** The owner confirms that those fingerprints are theirs, chooses a time and switches
 *      on. The request names exactly the confirmed fingerprints; the Pi refuses if its stored
 *      keys are any others.
 *
 * After the time, the Pi switches SSH off by itself, open connections included. While it is on,
 * this section says so in a visible notice with the end time, and offers "Jetzt ausschalten",
 * which needs nothing; forgetting the keys needs nothing either. On any other system the core
 * answers `available: false` and nothing is shown.
 *
 * The PIN is only ever a request field, as everywhere in this UI: sent when typed, cleared after
 * every attempt, never stored.
 */

import { type FormEvent, useCallback, useId, useState } from 'react';

import { failureKind, ipcReason } from '../../../../shared/errors';
import { isRecord } from '../../../../shared/guards';
import { refusalText, useRefreshOnConnect, useWhileVisible } from '../../hooks';
import { type Key, type Lang, type T, fill } from '../../i18n';
import { type IpcClient, api } from '../../ipc';

export interface SshKeyView {
  type: string;
  fingerprint: string;
  comment: string;
}

export interface SshStatus {
  on: boolean;
  until: string | null;
  keys: SshKeyView[];
  activeKeys: string[];
  defaultMinutes: number;
  minMinutes: number;
  maxMinutes: number;
}

/** How long, offered as choices (within the core's bounds). */
const DURATIONS = [15, 30, 60, 120, 240];
/** While on: refresh now and then, so the notice ends when SSH does. */
export const SSH_REFRESH_MS = 30000;

/** `noxos.ssh.*` reply -> status; `null` when this is no Nox-OS (or the reply is unreadable). */
export function parseSshStatus(payload: unknown): SshStatus | null {
  if (!isRecord(payload) || payload.available !== true) return null;
  const keys = Array.isArray(payload.keys)
    ? payload.keys.filter(isRecord).map((k) => ({
        type: typeof k.type === 'string' ? k.type : '',
        fingerprint: typeof k.fingerprint === 'string' ? k.fingerprint : '',
        comment: typeof k.comment === 'string' ? k.comment : '',
      }))
    : [];
  const num = (v: unknown, fallback: number) => (typeof v === 'number' && Number.isFinite(v) ? v : fallback);
  return {
    on: payload.on === true,
    until: typeof payload.until === 'string' ? payload.until : null,
    keys: keys.filter((k) => k.fingerprint),
    activeKeys: Array.isArray(payload.active_keys)
      ? payload.active_keys.filter((k): k is string => typeof k === 'string')
      : [],
    defaultMinutes: num(payload.default_minutes, 60),
    minMinutes: num(payload.min_minutes, 5),
    maxMinutes: num(payload.max_minutes, 240),
  };
}

/** The core's refusal reasons this section words itself. */
const REASONS: Record<string, Key> = {
  invalid_key: 'ssh_err_key',
  no_key: 'ssh_err_no_key',
  invalid_fingerprint: 'ssh_err_helper',
  helper_refused: 'ssh_err_helper',
  helper_unreachable: 'ssh_err_helper',
};

function clock(iso: string | null, lang: Lang): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString(lang === 'de' ? 'de-DE' : 'en-GB', { hour: '2-digit', minute: '2-digit' });
}

export interface RemoteMaintenanceProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
}

type Busy = 'keys' | 'on' | 'off' | 'forget' | null;

export function RemoteMaintenance({ t, lang, client }: RemoteMaintenanceProps) {
  const id = useId();
  const [status, setStatus] = useState<SshStatus | null>(null);
  const [pinSet, setPinSet] = useState(false);
  const [keys, setKeys] = useState('');
  const [keysPin, setKeysPin] = useState('');
  const [minutes, setMinutes] = useState(60);
  const [pin, setPin] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [replacing, setReplacing] = useState(false);
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState('');

  const load = useCallback(async (c: IpcClient, cancelled: () => boolean = () => false) => {
    try {
      const [reply, pinStatus] = await Promise.all([
        api.sshStatus(c),
        api.pinStatus(c).catch(() => ({ configured: false })),
      ]);
      if (cancelled()) return;
      const parsed = parseSshStatus(reply);
      setStatus(parsed);
      if (parsed) setMinutes((m) => (m === 60 ? parsed.defaultMinutes : m));
      setPinSet(isRecord(pinStatus) && pinStatus.configured === true);
    } catch (e) {
      if (cancelled()) return;
      // An older core, or none of this here: show nothing rather than a broken section.
      if (failureKind(e) === 'unknown_request') setStatus(null);
    }
  }, []);

  useRefreshOnConnect(client, load);
  useWhileVisible(client !== null && status?.on === true, SSH_REFRESH_MS, () => {
    if (client) void load(client);
  });

  if (status === null) return null;

  const describe = (e: unknown, pinSent: boolean): string => {
    const own = REASONS[ipcReason(e)];
    return own ? t(own) : refusalText(t, e, pinSent);
  };

  /** Runs one request; a new status replaces the shown one, a refusal is worded. */
  const act = async (label: Exclude<Busy, null>, sentPin: string, fn: (c: IpcClient) => Promise<unknown>) => {
    if (!client || busy) return false;
    setBusy(label);
    setError('');
    try {
      const parsed = parseSshStatus(await fn(client));
      if (parsed) setStatus(parsed);
      return true;
    } catch (err) {
      setError(describe(err, sentPin !== ''));
      return false;
    } finally {
      setBusy(null);
    }
  };

  const saveKeys = async (e: FormEvent) => {
    e.preventDefault();
    const sentPin = keysPin.trim();
    const ok = await act('keys', sentPin, (c) => api.sshKeysSet(c, keys.trim(), sentPin));
    setKeysPin('');
    if (ok) {
      setKeys('');
      setReplacing(false);
      setConfirmed(false); // new keys: their fingerprints are to be checked anew
    }
  };

  const switchOn = async (e: FormEvent) => {
    e.preventDefault();
    const sentPin = pin.trim();
    const fingerprints = status.keys.map((k) => k.fingerprint);
    await act('on', sentPin, (c) => api.sshEnable(c, minutes, fingerprints, sentPin));
    setPin('');
  };

  const durations = DURATIONS.filter((m) => m >= status.minMinutes && m <= status.maxMinutes);
  const hasKeys = status.keys.length > 0;
  const showKeyForm = !status.on && (!hasKeys || replacing);

  const pinField = (fieldId: string, value: string, onChange: (v: string) => void) => (
    <div className="field">
      <label htmlFor={fieldId} className="label">
        {t('lan_pair_pin')}
      </label>
      <input
        id={fieldId}
        className="input input--pin"
        type="password"
        inputMode="numeric"
        autoComplete="off"
        value={value}
        disabled={busy !== null}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );

  return (
    <section className="sec" aria-labelledby={`${id}-h`}>
      <h2 id={`${id}-h`} className="sec-title">
        {t('ssh_title')}
      </h2>
      <p className="lede">{t('ssh_lede')}</p>
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      {status.on ? (
        <div className="note note--warn" role="status">
          <p>
            <strong>{fill(t('ssh_on_until'), clock(status.until, lang))}</strong>
          </p>
          <p>{t('ssh_on_detail')}</p>
          {status.activeKeys.length > 0 && (
            <ul className="fact-list" aria-label={t('ssh_keys_active')}>
              {status.activeKeys.map((fp) => (
                <li key={fp} className="mono break">
                  {fp}
                </li>
              ))}
            </ul>
          )}
          <button
            type="button"
            className="btn"
            disabled={!client || busy !== null}
            onClick={() => void act('off', '', (c) => api.sshDisable(c))}
          >
            {busy === 'off' ? t('ssh_turning_off') : t('ssh_off_now')}
          </button>
        </div>
      ) : (
        <p role="status" className="hint">
          {t('ssh_off_state')}
        </p>
      )}

      {!pinSet && !status.on && <p className="hint">{t('ssh_needs_pin')}</p>}

      {!status.on && hasKeys && !replacing && (
        <form noValidate onSubmit={(e) => void switchOn(e)}>
          <fieldset className="fieldset-plain">
            <legend className="label">{t('ssh_keys_saved')}</legend>
            <ul className="fact-list">
              {status.keys.map((k) => (
                <li key={k.fingerprint}>
                  <span className="mono break">{k.fingerprint}</span>
                  {k.comment && <span className="muted"> {k.comment}</span>}
                </li>
              ))}
            </ul>
            <label className="caps-check field--spaced">
              <input
                type="checkbox"
                checked={confirmed}
                disabled={busy !== null}
                aria-describedby={`${id}-confirm-hint`}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              <span>{t('ssh_confirm_keys')}</span>
            </label>
            <p id={`${id}-confirm-hint`} className="hint">
              {t('ssh_confirm_hint')}
            </p>
          </fieldset>
          <div className="row-controls">
            <div className="field">
              <label htmlFor={`${id}-minutes`} className="label">
                {t('ssh_minutes_label')}
              </label>
              <select
                id={`${id}-minutes`}
                className="input"
                value={minutes}
                disabled={busy !== null}
                onChange={(e) => setMinutes(Number(e.target.value))}
              >
                {durations.map((m) => (
                  <option key={m} value={m}>
                    {m < 60 ? fill(t('ssh_minutes'), m) : fill(t('ssh_hours'), m / 60)}
                  </option>
                ))}
              </select>
            </div>
            {pinSet && pinField(`${id}-pin`, pin, setPin)}
            <button
              type="submit"
              className="btn"
              disabled={!client || busy !== null || !pinSet || !confirmed || pin.trim() === ''}
            >
              {busy === 'on' ? t('ssh_turning_on') : t('ssh_on')}
            </button>
          </div>
          <div className="row-controls">
            <button
              type="button"
              className="btn btn--sm btn--plain"
              disabled={busy !== null}
              onClick={() => setReplacing(true)}
            >
              {t('ssh_keys_other')}
            </button>
            <button
              type="button"
              className="btn btn--sm btn--plain"
              disabled={!client || busy !== null}
              onClick={() => void act('forget', '', (c) => api.sshDisable(c, true))}
            >
              {t('ssh_keys_forget')}
            </button>
          </div>
        </form>
      )}

      {showKeyForm && (
        <form noValidate onSubmit={(e) => void saveKeys(e)}>
          <div className="field field--spaced">
            <label htmlFor={`${id}-keys`} className="label">
              {hasKeys ? t('ssh_keys_replace') : t('ssh_keys_label')}
            </label>
            <textarea
              id={`${id}-keys`}
              className="textarea mono"
              rows={3}
              spellCheck={false}
              autoComplete="off"
              aria-describedby={`${id}-keys-hint`}
              placeholder="ssh-ed25519 AAAA… name@example.invalid"
              value={keys}
              disabled={busy !== null}
              onChange={(e) => setKeys(e.target.value)}
            />
            <p id={`${id}-keys-hint`} className="hint">
              {t('ssh_keys_hint')}
            </p>
          </div>
          <div className="row-controls">
            {pinSet && pinField(`${id}-keys-pin`, keysPin, setKeysPin)}
            <button
              type="submit"
              className="btn"
              disabled={!client || busy !== null || !pinSet || keys.trim() === '' || keysPin.trim() === ''}
            >
              {busy === 'keys' ? t('ssh_keys_saving') : t('ssh_keys_save')}
            </button>
            {hasKeys && (
              <button type="button" className="btn btn--sm btn--plain" onClick={() => setReplacing(false)}>
                {t('ssh_keys_cancel')}
              </button>
            )}
          </div>
        </form>
      )}
    </section>
  );
}
