/**
 * The two Status-page controls that loosen protection, and therefore ask before they act.
 *
 * `PrivacyTile` - choosing a less private mode. Moving to "Alles erlaubt" shows a confirmation
 * first (the core refuses FULL without `confirmed`, and used to do so silently while the page
 * reported success); a relaxing change while a PIN is set shows a PIN field. The answer is read,
 * not assumed: `applied: false` is said out loud.
 *
 * `ResumeTile` - leaving safe mode. Visible only while safe mode is on. The core decides whether
 * the PIN is needed (a security-path kill while a PIN is set) and says so in `security.pin.status`
 * before the attempt, and in the refusal after it; either way the PIN field appears. A successful
 * resume also re-arms the supervisor's watchdog; if the core could not reach it, the page says so.
 *
 * The PIN is component state for one request and is cleared after it - never stored, never sent
 * with anything but the change the user just asked for.
 */

import { useState } from 'react';

import { errorText } from '../../../shared/errors';
import { type Lang, type T, pinRefusalText, privacyLabel } from '../i18n';
import { type IpcClient, api } from '../ipc';
import {
  PRIVACY_MODES,
  type PinRefusal,
  type PinState,
  parsePrivacySetResult,
  parseResumeResult,
  pinRefusal,
  relaxesPrivacy,
} from '../model';
import { Tile } from '../ui';

interface PinFieldProps {
  id: string;
  label: string;
  hint?: string;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}

function PinField({ id, label, hint, value, disabled, onChange }: PinFieldProps) {
  return (
    <div className="field">
      <label htmlFor={id} className="label">
        {label}
      </label>
      <input
        id={id}
        type="password"
        className="input"
        autoComplete="off"
        inputMode="text"
        disabled={disabled}
        aria-describedby={hint ? `${id}-hint` : undefined}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      {hint && (
        <p id={`${id}-hint`} className="hint">
          {hint}
        </p>
      )}
    </div>
  );
}

// ---- privacy -----------------------------------------------------------------------------------

type PrivacyNote = { tone: 'ok' | 'error'; text: string } | null;

export interface PrivacyTileProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  /** The mode the core last reported. */
  current: string | null;
  pin: PinState | null;
}

export function PrivacyTile({ t, lang, client, current, pin }: PrivacyTileProps) {
  /** Only an explicit choice lives here; `null` means "show whatever the core last reported". */
  const [choice, setChoice] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [pinValue, setPinValue] = useState('');
  /** The core asked for the PIN even though the status did not say so (it can change). */
  const [pinAsked, setPinAsked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<PrivacyNote>(null);

  const disabled = client === null;
  const value = choice ?? current ?? '';
  const relaxing = value !== '' && relaxesPrivacy(current, value);
  const needsPin = relaxing && ((pin?.gateRequired ?? false) || pinAsked);
  const needsConfirm = value === 'full' && current !== 'full';

  const send = async (confirmed: boolean) => {
    if (!client || busy || !value) return;
    setBusy(true);
    setNote(null);
    try {
      const result = parsePrivacySetResult(
        await api.setPrivacy(client, value, { confirmed, pin: needsPin ? pinValue : '' }),
      );
      if (result.applied) {
        setChoice(null);
        setNote({ tone: 'ok', text: t('privacy_applied') });
        setPinAsked(false);
      } else {
        setNote({ tone: 'error', text: t('privacy_not_applied') });
      }
      setConfirming(false);
    } catch (e) {
      const refusal: PinRefusal | null = pinRefusal(e);
      if (refusal?.reason === 'pin_required' || refusal?.reason === 'pin_wrong') setPinAsked(true);
      setNote({
        tone: 'error',
        text: refusal ? pinRefusalText(t, lang, refusal) : errorText(t('error_prefix'), e),
      });
    } finally {
      setPinValue('');
      setBusy(false);
    }
  };

  const apply = () => {
    if (needsConfirm) {
      setConfirming(true);
      setNote(null);
      return;
    }
    void send(false);
  };

  return (
    <Tile id="privacy" title={t('privacy_title')}>
      <div className="field">
        <label htmlFor="privacy-mode" className="label">
          {t('privacy_mode')}
        </label>
        <select
          id="privacy-mode"
          className="select"
          value={value}
          disabled={disabled}
          onChange={(e) => {
            setChoice(e.target.value);
            setConfirming(false);
            setNote(null);
          }}
        >
          {current === null && <option value="">{t('unknown')}</option>}
          {PRIVACY_MODES.map((m) => (
            <option key={m} value={m}>
              {privacyLabel(t, m)}
            </option>
          ))}
        </select>
      </div>
      {needsPin && (
        <PinField
          id="privacy-pin"
          label={t('privacy_pin_label')}
          hint={t('privacy_pin_hint')}
          value={pinValue}
          disabled={disabled || busy}
          onChange={setPinValue}
        />
      )}
      {confirming ? (
        <div className="confirm-box" role="group" aria-labelledby="privacy-confirm-title">
          <p id="privacy-confirm-title" className="confirm-box-title">
            {t('privacy_full_confirm_title')}
          </p>
          <p className="hint">{t('privacy_full_confirm_text')}</p>
          <div className="tile-actions">
            <button
              type="button"
              className="btn"
              disabled={disabled || busy || (needsPin && pinValue === '')}
              onClick={() => void send(true)}
            >
              {t('privacy_full_confirm_button')}
            </button>
            <button type="button" className="btn btn--quiet" onClick={() => setConfirming(false)}>
              {t('privacy_cancel')}
            </button>
          </div>
        </div>
      ) : (
        <div className="tile-actions">
          <button
            type="button"
            className="btn"
            disabled={
              disabled || busy || !value || value === current || (needsPin && pinValue === '')
            }
            onClick={apply}
          >
            {t('privacy_apply')}
          </button>
        </div>
      )}
      {note && (
        <p role={note.tone === 'error' ? 'alert' : 'status'} className={note.tone === 'ok' ? 'ok-note' : 'setting-error'}>
          {note.text}
        </p>
      )}
    </Tile>
  );
}

// ---- resume ------------------------------------------------------------------------------------

export interface ResumeTileProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  pin: PinState | null;
  /**
   * Called after a successful resume with what the supervisor answered (`rearmed`, `unreachable`,
   * ...). This tile disappears with safe mode, so the page says the outcome, not the tile.
   */
  onResumed: (supervisor: string) => void;
}

export function ResumeTile({ t, lang, client, pin, onResumed }: ResumeTileProps) {
  const [pinValue, setPinValue] = useState('');
  const [pinAsked, setPinAsked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const disabled = client === null;
  const needsPin = (pin?.resumeRequiresPin ?? false) || pinAsked;

  const resume = async () => {
    if (!client || busy) return;
    setBusy(true);
    setError('');
    try {
      const result = parseResumeResult(await api.resume(client, needsPin ? pinValue : ''));
      if (result.ok) {
        setPinAsked(false);
        onResumed(result.supervisor);
      } else if (result.refusal) {
        const reason = result.refusal.reason;
        if (reason === 'pin_required' || reason === 'pin_wrong') setPinAsked(true);
        setError(pinRefusalText(t, lang, result.refusal));
      } else {
        setError(t('error_prefix'));
      }
    } catch (e) {
      setError(errorText(t('error_prefix'), e));
    } finally {
      setPinValue('');
      setBusy(false);
    }
  };

  return (
    <Tile feature id="resume" eyebrow={t('kill_engaged')} title={t('resume_title')} lede={t('resume_explain')}>
      {needsPin && <p className="hint">{t('resume_explain_pin')}</p>}
      {needsPin && (
        <PinField
          id="resume-pin"
          label={t('resume_pin_label')}
          value={pinValue}
          disabled={disabled || busy}
          onChange={setPinValue}
        />
      )}
      <div className="tile-actions">
        <button
          type="button"
          className="btn"
          disabled={disabled || busy || (needsPin && pinValue === '')}
          onClick={() => void resume()}
        >
          {t('resume_button')}
        </button>
      </div>
      {error && (
        <p role="alert" className="setting-error">
          {error}
        </p>
      )}
    </Tile>
  );
}
