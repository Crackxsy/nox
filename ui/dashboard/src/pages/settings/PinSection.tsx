/**
 * "Sicherheits-PIN": set the first PIN, change it, remove it.
 *
 * The rules are the core's (`nox.security.pin_setup`): the first PIN needs nothing, changing or
 * removing needs the current one, too short is refused, a broken entry is never touched from here.
 * This tile only mirrors them so the user sees the right fields, checks the two new entries agree
 * before anything is sent, and turns every refusal into a sentence - how many attempts are left,
 * until when a lockout runs, that the stored entry has to be repaired with `nox pin set`.
 *
 * Removing asks twice: without a PIN, relaxing privacy is one click again, and that should not
 * happen by a slip. The PINs live in component state for one request and are cleared after it.
 */

import { useState } from 'react';

import { errorText } from '../../../../shared/errors';
import { formatTimestamp } from '../../../../shared/format';
import { type Lang, type T, fill, pinRefusalText } from '../../i18n';
import { type IpcClient, api } from '../../ipc';
import { pinRefusal } from '../../model';
import { Tile } from '../../ui';
import type { PinStatusView } from '../usePinStatus';

export interface PinSectionProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  status: PinStatusView;
  /** After a change: the Settings page re-reads what depends on the PIN (credential fields). */
  onChanged: () => void;
}

type Note = { tone: 'ok' | 'error'; text: string } | null;

interface FieldProps {
  id: string;
  label: string;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
  describedBy?: string;
}

function SecretInput({ id, label, value, disabled, onChange, describedBy }: FieldProps) {
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
        disabled={disabled}
        aria-describedby={describedBy}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

export function PinSection({ t, lang, client, status, onChanged }: PinSectionProps) {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [repeat, setRepeat] = useState('');
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<Note>(null);

  const pin = status.pin;
  const disabled = client === null || busy;
  const minLength = pin?.minLength ?? 6;
  const isSet = pin?.state === 'valid';

  const clear = () => {
    setCurrent('');
    setNext('');
    setRepeat('');
  };

  const fail = (e: unknown) => {
    const refusal = pinRefusal(e);
    setNote({
      tone: 'error',
      text: refusal ? pinRefusalText(t, lang, refusal) : errorText(t('error_prefix'), e),
    });
  };

  const save = async () => {
    if (!client || busy) return;
    if (next.length < minLength) {
      setNote({ tone: 'error', text: fill(t('pin_refusal_too_short'), minLength) });
      return;
    }
    if (next !== repeat) {
      setNote({ tone: 'error', text: t('pin_mismatch') });
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      await api.pinSet(client, next, isSet ? current : '');
      setNote({ tone: 'ok', text: t('pin_saved') });
      await status.refresh();
      onChanged();
    } catch (e) {
      fail(e);
    } finally {
      clear();
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!client || busy) return;
    setBusy(true);
    setNote(null);
    try {
      await api.pinClear(client, current);
      setNote({ tone: 'ok', text: t('pin_removed') });
      setConfirmRemove(false);
      await status.refresh();
      onChanged();
    } catch (e) {
      fail(e);
    } finally {
      clear();
      setBusy(false);
    }
  };

  return (
    <Tile id="settings-pin" title={t('pin_section_title')} lede={t('pin_section_lede')}>
      {pin === null && !status.failed && (
        <div aria-hidden="true">
          <div className="skeleton skeleton-row" />
          <div className="skeleton skeleton-row" />
        </div>
      )}
      {pin === null && status.failed && (
        <div className="tile-actions">
          <p className="muted">{t('pin_state_failed')}</p>
          <button
            type="button"
            className="link"
            disabled={client === null}
            onClick={() => void status.refresh()}
          >
            {t('chat_history_retry')}
          </button>
        </div>
      )}
      {pin !== null && (
        <p role="status" className={isSet ? 'ok-note' : 'hint'}>
          {pin.state === 'valid'
            ? t('pin_state_valid')
            : pin.state === 'invalid'
              ? t('pin_state_invalid')
              : pin.state === 'unavailable'
                ? t('pin_state_unavailable')
                : t('pin_state_not_set')}
        </p>
      )}
      {pin?.lockedUntil && (
        <p className="setting-error">
          {fill(t('pin_state_locked'), formatTimestamp(pin.lockedUntil, lang))}
        </p>
      )}

      {/* One set of fields at a time: the removal box has its own "current PIN" field. */}
      {pin !== null && (pin.state === 'valid' || pin.state === 'not_set') && !confirmRemove && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          {isSet && (
            <SecretInput
              id="pin-current"
              label={t('pin_current_label')}
              value={current}
              disabled={disabled}
              onChange={setCurrent}
            />
          )}
          <SecretInput
            id="pin-new"
            label={t('pin_new_label')}
            value={next}
            disabled={disabled}
            onChange={setNext}
            describedBy="pin-new-hint"
          />
          <p id="pin-new-hint" className="hint">
            {fill(t('pin_new_hint'), minLength)}
          </p>
          <SecretInput
            id="pin-repeat"
            label={t('pin_confirm_label')}
            value={repeat}
            disabled={disabled}
            onChange={setRepeat}
          />
          <div className="tile-actions">
            <button
              type="submit"
              className="btn"
              disabled={disabled || next === '' || repeat === '' || (isSet && current === '')}
            >
              {isSet ? t('pin_change_button') : t('pin_set_button')}
            </button>
            {isSet && (
              <button
                type="button"
                className="link link--plain"
                disabled={disabled}
                onClick={() => {
                  setConfirmRemove(true);
                  setNote(null);
                  clear();
                }}
              >
                {t('pin_remove_button')}
              </button>
            )}
          </div>
        </form>
      )}

      {isSet && confirmRemove && (
        <div className="confirm-box" role="group" aria-labelledby="pin-remove-title">
          <p id="pin-remove-title" className="confirm-box-title">
            {t('pin_remove_button')}
          </p>
          <p className="hint">{t('pin_remove_explain')}</p>
          <SecretInput
            id="pin-remove-current"
            label={t('pin_current_label')}
            value={current}
            disabled={disabled}
            onChange={setCurrent}
          />
          <div className="tile-actions">
            <button
              type="button"
              className="btn btn--danger"
              disabled={disabled || current === ''}
              onClick={() => void remove()}
            >
              {t('pin_remove_confirm_button')}
            </button>
            <button
              type="button"
              className="btn btn--quiet"
              onClick={() => {
                setConfirmRemove(false);
                clear();
              }}
            >
              {t('privacy_cancel')}
            </button>
          </div>
        </div>
      )}

      {note && (
        <p
          role={note.tone === 'error' ? 'alert' : 'status'}
          className={note.tone === 'ok' ? 'ok-note' : 'setting-error'}
        >
          {note.text}
        </p>
      )}
    </Tile>
  );
}
