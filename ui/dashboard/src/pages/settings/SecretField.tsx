/**
 * One credential row: a write-only password field, or — when the core says the name is set — the
 * word "gespeichert" plus Change and Delete.
 *
 * Secrets are write-only by construction: `secrets.status` says whether a name is present, never
 * what it holds, so nothing here can render a masked value that could be copied out.
 *
 * The PIN prompt belongs to *one* secret at a time. Rendering it under every visible secret meant
 * that typing a PIN filled three inputs at once and announced the same `role="alert"` three times,
 * because all three were bound to one piece of state. It is now shown only for the secret the user
 * is acting on, and cleared the moment they act on a different one.
 */

import type { Key, T } from '../../i18n';

export interface SecretFieldProps {
  t: T;
  name: string;
  labelKey: Key;
  hintKey?: Key;
  present: boolean;
  editing: boolean;
  onEditing: (editing: boolean) => void;
  value: string;
  onValue: (value: string) => void;
  busy: boolean;
  disabled: boolean;
  /** True when this secret is the one a PIN is being typed for. */
  pinActive: boolean;
  pinConfigured: boolean;
  pin: string;
  onPin: (pin: string) => void;
  /** `pin_required` / `pin_wrong`, or '' when there is nothing to say. */
  pinMessage: Key | '';
  onSave: () => void;
  onDelete: () => void;
}

export function SecretField({
  t,
  name,
  labelKey,
  hintKey,
  present,
  editing,
  onEditing,
  value,
  onValue,
  busy,
  disabled,
  pinActive,
  pinConfigured,
  pin,
  onPin,
  pinMessage,
  onSave,
  onDelete,
}: SecretFieldProps) {
  const id = `set-${name.replace(/[^a-zA-Z0-9]+/g, '-')}`;
  const pinId = `${id}-pin`;

  const pinField = pinConfigured && pinActive && (
    <div className="field">
      <label htmlFor={pinId} className="label">
        {t('pin_label')}
      </label>
      <input
        id={pinId}
        type="password"
        className="input"
        autoComplete="off"
        disabled={disabled || busy}
        aria-describedby={`${pinId}-hint`}
        value={pin}
        onChange={(e) => onPin(e.target.value)}
      />
      <p id={`${pinId}-hint`} className="hint">
        {t('pin_hint')}
      </p>
      {pinMessage && (
        <p role="alert" className="setting-error">
          {t(pinMessage)}
        </p>
      )}
    </div>
  );

  if (present && !editing) {
    return (
      <div className="field field--spaced">
        <span className="label">{t(labelKey)}</span>
        <div className="tile-actions">
          <span className="state state--ok">{t('secret_stored')}</span>
          <button
            type="button"
            className="link"
            disabled={disabled || busy}
            onClick={() => onEditing(true)}
          >
            {t('secret_change')}
          </button>
          <button
            type="button"
            className="link link--plain"
            disabled={disabled || busy}
            onClick={onDelete}
          >
            {t('secret_delete')}
          </button>
        </div>
        {pinField}
      </div>
    );
  }

  return (
    <div className="field field--spaced">
      <label htmlFor={id} className="label">
        {t(labelKey)}
      </label>
      <input
        id={id}
        type="password"
        className="input"
        autoComplete="off"
        disabled={disabled || busy}
        aria-describedby={hintKey ? `${id}-hint` : undefined}
        value={value}
        onChange={(e) => onValue(e.target.value)}
      />
      {!present && <p className="hint">{t('secret_missing')}</p>}
      {hintKey && (
        <p id={`${id}-hint`} className="hint">
          {t(hintKey)}
        </p>
      )}
      {pinField}
      <div className="tile-actions">
        <button
          type="button"
          className="btn btn--sm"
          disabled={disabled || busy || value.trim() === ''}
          onClick={onSave}
        >
          {t('secret_save')}
        </button>
        {present && (
          <button type="button" className="link link--plain" onClick={() => onEditing(false)}>
            {t('secret_cancel')}
          </button>
        )}
      </div>
    </div>
  );
}
