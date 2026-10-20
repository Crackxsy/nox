/**
 * Recording consents: what `mic.record_long` needs before it may record other people.
 *
 * Recording someone's spoken words without their consent is a criminal offence in Germany
 * (§ 201 StGB), so the core refuses a long recording unless a consent for its context is stored.
 * This tile is the deliberate, human way to store one: the occasion key the recording will name,
 * who agreed, and the exact wording, which the core keeps verbatim together with the time and how
 * it was recorded. Every row can be withdrawn; withdrawing needs no PIN because it can only make
 * Nox stricter.
 *
 * The PIN follows the rule from the credentials tile: it lives in state for one request, is sent
 * only when typed, and is cleared afterwards whatever the outcome.
 */

import { type FormEvent, useCallback, useEffect, useState } from 'react';

import { errorText } from '../../../../shared/errors';
import { formatTimestamp, timestampTitle } from '../../../../shared/format';
import { isRecord, str } from '../../../../shared/guards';
import { type Key, type Lang, type T, fill } from '../../i18n';
import { type ConsentGrantRequest, type IpcClient, IpcError, api } from '../../ipc';
import { pinErrorKey } from '../../model';
import { Tile } from '../../ui';

export interface Consent {
  id: string;
  context: string;
  who: string;
  wording: string;
  grantedAt: string;
  recordedBy: string;
}

/** Field limits, the same as the core's `ConsentGrant` model. */
export const CONSENT_LIMITS: Readonly<Record<keyof ConsentGrantRequest, number>> = {
  context: 200,
  who: 200,
  wording: 2000,
};

/** `consent.list` reply -> consents; a row without context or wording is dropped, not guessed. */
export function parseConsents(payload: unknown): Consent[] {
  if (!isRecord(payload) || !Array.isArray(payload.consents)) return [];
  const out: Consent[] = [];
  for (const entry of payload.consents) {
    if (!isRecord(entry)) continue;
    const context = str(entry.context);
    const wording = str(entry.wording);
    if (!context || !wording) continue;
    out.push({
      id: typeof entry.id === 'number' ? String(entry.id) : str(entry.id, context),
      context,
      who: str(entry.who),
      wording,
      grantedAt: str(entry.granted_at),
      recordedBy: str(entry.recorded_by),
    });
  }
  return out;
}

/** Length in code points, which is what the core's limit counts. */
function length(value: string): number {
  return Array.from(value).length;
}

/**
 * The trimmed request, or null while a field is empty or too long. `tooLong` names the fields that
 * exceed their limit so each can say so next to itself.
 */
export function validateConsent(draft: ConsentGrantRequest): {
  grant: ConsentGrantRequest | null;
  tooLong: (keyof ConsentGrantRequest)[];
} {
  const trimmed: ConsentGrantRequest = {
    context: draft.context.trim(),
    who: draft.who.trim(),
    wording: draft.wording.trim(),
  };
  const fields = Object.keys(CONSENT_LIMITS) as (keyof ConsentGrantRequest)[];
  const tooLong = fields.filter((f) => length(trimmed[f]) > CONSENT_LIMITS[f]);
  const empty = fields.some((f) => trimmed[f] === '');
  return { grant: empty || tooLong.length > 0 ? null : trimmed, tooLong };
}

const EMPTY_DRAFT: ConsentGrantRequest = { context: '', who: '', wording: '' };

export interface ConsentTileProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  /** Whether the core has a security PIN; a grant then has to carry it. */
  pinConfigured: boolean;
}

export function ConsentTile({ t, lang, client, pinConfigured }: ConsentTileProps) {
  const [consents, setConsents] = useState<Consent[] | null>(null);
  const [draft, setDraft] = useState<ConsentGrantRequest>(EMPTY_DRAFT);
  const [pin, setPin] = useState('');
  const [pinMessage, setPinMessage] = useState<Key | ''>('');
  const [busy, setBusy] = useState<'grant' | 'withdraw' | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState<Key | ''>('');

  const load = useCallback(async () => {
    if (!client) return;
    try {
      setConsents(parseConsents(await api.consentList(client)));
      setError('');
    } catch (e) {
      setError(errorText(t('error_prefix'), e));
    }
  }, [client, t]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() sets state only after awaiting the core
    void load();
  }, [load]);

  const { grant, tooLong } = validateConsent(draft);
  const disabled = client === null;

  const update = (field: keyof ConsentGrantRequest, value: string) => {
    setDraft((d) => ({ ...d, [field]: value }));
    setNotice('');
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!client || busy || !grant) return;
    const sentPin = pin;
    setBusy('grant');
    setError('');
    setNotice('');
    setPinMessage('');
    try {
      await api.consentGrant(client, grant, sentPin);
      setDraft(EMPTY_DRAFT);
      setNotice('consent_granted');
      await load();
    } catch (e) {
      // A refusal that names the PIN gets the PIN message; anything else keeps the core's wording.
      const key = e instanceof IpcError ? pinErrorKey(e.code, e.message, sentPin !== '') : null;
      if (key) setPinMessage(key);
      else setError(errorText(t('error_prefix'), e));
    } finally {
      setPin('');
      setBusy(null);
    }
  };

  const withdraw = async (context: string) => {
    if (!client || busy) return;
    setBusy('withdraw');
    setError('');
    setNotice('');
    try {
      await api.consentRevoke(client, context);
      setNotice('consent_withdrawn');
      await load();
    } catch (e) {
      setError(errorText(t('error_prefix'), e));
    } finally {
      setBusy(null);
    }
  };

  // The PIN field also appears when the core asked for a PIN the status request did not report.
  const showPin = pinConfigured || pinMessage !== '';

  const field = (
    name: keyof ConsentGrantRequest,
    labelKey: Key,
    hintKey: Key | null,
    placeholderKey: Key | null,
    multiline = false,
  ) => {
    const id = `consent-${name}`;
    const over = tooLong.includes(name);
    const describedBy = [hintKey ? `${id}-hint` : '', over ? `${id}-error` : '']
      .filter(Boolean)
      .join(' ');
    const common = {
      id,
      value: draft[name],
      disabled: disabled || busy !== null,
      required: true,
      'aria-invalid': over || undefined,
      'aria-describedby': describedBy || undefined,
      placeholder: placeholderKey ? t(placeholderKey) : undefined,
    };
    return (
      <div className="field field--spaced">
        <label htmlFor={id} className="label">
          {t(labelKey)}
        </label>
        {multiline ? (
          <textarea
            {...common}
            className="textarea"
            rows={4}
            onChange={(e) => update(name, e.target.value)}
          />
        ) : (
          <input
            {...common}
            className="input"
            autoComplete="off"
            onChange={(e) => update(name, e.target.value)}
          />
        )}
        {hintKey && (
          <p id={`${id}-hint`} className="hint">
            {t(hintKey)}
          </p>
        )}
        {over && (
          <p id={`${id}-error`} className="setting-error">
            {fill(t('consent_too_long'), CONSENT_LIMITS[name])}
          </p>
        )}
      </div>
    );
  };

  return (
    <Tile id="consents" title={t('consent_title')} lede={t('consent_hint')}>
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}
      {consents === null ? (
        <p className="muted">{t('consent_loading')}</p>
      ) : consents.length === 0 ? (
        <p className="muted">{t('consent_empty')}</p>
      ) : (
        <ul className="fact-list" aria-label={t('consent_list_label')}>
          {consents.map((c) => (
            <li key={c.id} className="consent-row">
              <div className="consent-head">
                <span className="consent-context">{c.context}</span>
                <button
                  type="button"
                  className="btn btn--quiet btn--sm"
                  disabled={disabled || busy !== null}
                  onClick={() => void withdraw(c.context)}
                  aria-label={`${t('consent_withdraw')}: ${c.context}`}
                >
                  {t('consent_withdraw')}
                </button>
              </div>
              <blockquote className="consent-wording">{c.wording}</blockquote>
              <dl className="consent-facts">
                <div>
                  <dt>{t('consent_who')}</dt>
                  <dd>{c.who}</dd>
                </div>
                <div>
                  <dt>{t('consent_when')}</dt>
                  <dd title={timestampTitle(c.grantedAt)}>{formatTimestamp(c.grantedAt, lang)}</dd>
                </div>
                <div>
                  <dt>{t('consent_recorded_by')}</dt>
                  <dd>{c.recordedBy}</dd>
                </div>
              </dl>
            </li>
          ))}
        </ul>
      )}

      <form noValidate onSubmit={(e) => void submit(e)}>
        {field(
          'context',
          'consent_context_label',
          'consent_context_hint',
          'consent_context_placeholder',
        )}
        {field('who', 'consent_who_label', null, 'consent_who_placeholder')}
        {field('wording', 'consent_wording_label', 'consent_wording_hint', null, true)}
        <p className="hint">{t('consent_record_note')}</p>
        {showPin && (
          <div className="field field--spaced">
            <label htmlFor="consent-pin" className="label">
              {t('consent_pin_label')}
            </label>
            <input
              id="consent-pin"
              type="password"
              className="input"
              autoComplete="off"
              disabled={disabled || busy !== null}
              aria-describedby="consent-pin-hint"
              value={pin}
              onChange={(e) => {
                setPin(e.target.value);
                setPinMessage('');
              }}
            />
            <p id="consent-pin-hint" className="hint">
              {t('consent_pin_hint')}
            </p>
            {pinMessage && (
              <p role="alert" className="setting-error">
                {t(pinMessage)}
              </p>
            )}
          </div>
        )}
        <div className="tile-actions">
          <button
            type="submit"
            className="btn"
            disabled={disabled || busy !== null || grant === null}
          >
            {busy === 'grant' ? t('consent_granting') : t('consent_grant')}
          </button>
          {!grant && tooLong.length === 0 && <p className="hint">{t('consent_required')}</p>}
          {notice && (
            <p role="status" className="ok-note">
              {t(notice)}
            </p>
          )}
        </div>
      </form>
    </Tile>
  );
}
