/**
 * Small pieces the three "Fähigkeiten" sections share: a two-step confirm for the actions that
 * cannot be undone, a reload hook that fires on connect *and* on "Aktualisieren", and the line a
 * failed load turns into.
 */

import { type KeyboardEvent, useEffect, useRef, useState } from 'react';

import { errorText, failureKind } from '../../../../shared/errors';
import type { T } from '../../i18n';
import type { IpcClient } from '../../ipc';

/** The sentence for a list request that failed; a core without the extension says so plainly. */
export function loadError(t: T, e: unknown): string {
  return failureKind(e) === 'unknown_request'
    ? t('caps_not_installed')
    : errorText(t('caps_load_failed'), e);
}

/**
 * `useRefreshOnConnect` plus a revision: runs `load` once per connection and again whenever
 * `revision` moves, cancelling the previous run's writes.
 */
export function useReload(
  client: IpcClient | null,
  revision: number,
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
  }, [client, revision]);
}

export interface ConfirmActionProps {
  t: T;
  /** The button that starts it ("Entfernen", "Vergessen"). */
  label: string;
  /** The question shown once armed. */
  question: string;
  /** The button that does it ("Ja, entfernen"). */
  confirmLabel: string;
  disabled: boolean;
  onConfirm: () => void;
}

/**
 * Removing a server or forgetting a skill cannot be undone, so it takes two clicks: the first one
 * asks, the second one acts. Focus moves to the confirm button so a keyboard user lands on the
 * question; Escape or "Abbrechen" steps back.
 */
export function ConfirmAction({ t, label, question, confirmLabel, disabled, onConfirm }: ConfirmActionProps) {
  const [armed, setArmed] = useState(false);
  const confirmRef = useRef<HTMLButtonElement | null>(null);
  const startRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (armed) confirmRef.current?.focus();
  }, [armed]);

  const cancel = () => {
    setArmed(false);
    // Back to where the user started, not to the top of the page.
    window.setTimeout(() => startRef.current?.focus(), 0);
  };

  const onEscape = (e: KeyboardEvent) => {
    if (e.key === 'Escape') cancel();
  };

  if (!armed) {
    return (
      <button
        ref={startRef}
        type="button"
        className="btn btn--quiet btn--sm"
        disabled={disabled}
        onClick={() => setArmed(true)}
      >
        {label}
      </button>
    );
  }
  return (
    <div className="confirm-row" role="group" aria-label={label}>
      <p className="hint" role="alert">
        {question}
      </p>
      <div className="tile-actions tile-actions--tight">
        <button
          ref={confirmRef}
          type="button"
          className="btn btn--danger btn--sm"
          disabled={disabled}
          onKeyDown={onEscape}
          onClick={() => {
            setArmed(false);
            onConfirm();
          }}
        >
          {confirmLabel}
        </button>
        <button
          type="button"
          className="btn btn--quiet btn--sm"
          onKeyDown={onEscape}
          onClick={cancel}
        >
          {t('caps_cancel')}
        </button>
      </div>
    </div>
  );
}

/** A PIN input, password-typed and never stored beyond the one request it is typed for. */
export function PinField({
  t,
  id,
  value,
  disabled,
  onChange,
  hint,
}: {
  t: T;
  id: string;
  value: string;
  disabled: boolean;
  onChange: (pin: string) => void;
  /** Why the PIN is asked for here. */
  hint: string;
}) {
  return (
    <div className="field field--spaced">
      <label htmlFor={id} className="label">
        {t('pin_label')}
      </label>
      <input
        id={id}
        type="password"
        className="input"
        autoComplete="off"
        disabled={disabled}
        aria-describedby={`${id}-hint`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      <p id={`${id}-hint`} className="hint">
        {hint}
      </p>
    </div>
  );
}
