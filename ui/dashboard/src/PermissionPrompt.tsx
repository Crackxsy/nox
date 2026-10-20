/**
 * "May Nox do this?": the permission prompt, on every dashboard a person looks at.
 *
 * The core asks every confirming client at once (`security.permission_requested`); the first answer
 * wins, and `security.permission_decided` takes the prompt away everywhere. A dashboard that did
 * not answer says how it ended: someone answered on another device, or nobody answered in time and
 * Nox did not do it (`PermissionNote`). A dashboard that (re)connects asks for the questions still
 * open (`security.permission.pending`), so a prompt is neither missed nor left standing stale.
 *
 * What it says: one plain sentence first ("Nox möchte löschen: Archiv/alt.zip"), the area and the
 * risk in words, how long is left before no answer means no - and the identifiers behind a
 * "technical details" disclosure for whoever wants them.
 *
 * Answers: Deny and Allow side by side, equal in size, in the order they are read; at high or
 * critical risk Allow is not the inviting blue button. On the machine Nox runs on, a quiet third
 * option allows for as long as the core keeps such a grant ("for 8 hours" - never "always"). A
 * paired device answers once, and allows at high or critical risk only with the security PIN; with
 * no PIN set it says where this can be allowed instead, and Deny still works.
 *
 * Accessibility: an `alertdialog` with a label and a description, focus on Deny (the answer that
 * can never do harm) or on the dialog itself while nothing can be pressed, Tab kept inside, the
 * page behind made `inert` by the app, Escape denies (once, also while a reply is in flight), and
 * focus goes back where it was afterwards. The countdown is announced at 30 and 10 seconds only.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { refusalText } from './hooks';
import {
  auditActionLabel,
  auditToolLabel,
  fill,
  type Key,
  rememberLabel,
  type T,
} from './i18n';
import { type IpcClient, api } from './ipc';
import { type DecidedNote, type PermissionPrompt, needsDevicePin, secondsLeft } from './model';

export interface PermissionPromptProps {
  t: T;
  client: IpcClient | null;
  queue: PermissionPrompt[];
  /** A paired device: no remembered grant, and the PIN at high or critical risk. */
  device: boolean;
  /** On a paired device: whether a security PIN is set; null while not known. */
  pinSet: boolean | null;
  /** Called with the prompt id once this dashboard answered (the core's event removes it too). */
  onAnswered: (id: string) => void;
  /** Injected in tests. */
  clock?: () => number;
}

const RISK_KEYS: Record<string, Key> = {
  read: 'perm_risk_read',
  low: 'perm_risk_low',
  medium: 'perm_risk_medium',
  high: 'perm_risk_high',
  critical: 'perm_risk_critical',
};

/** Seconds left at which the countdown is said out loud, once each. */
const ANNOUNCE_AT = [30, 10] as const;

function bucket(left: number | null): number | null {
  if (left === null || left <= 0) return null;
  let found: number | null = null;
  for (const at of ANNOUNCE_AT) if (left <= at) found = at;
  return found;
}

const FOCUSABLE = 'button:not(:disabled), input:not(:disabled), summary, a[href]';

export function PermissionPromptDialog(props: PermissionPromptProps) {
  const prompt = props.queue[0];
  const restoreRef = useRef<HTMLElement | null>(null);
  const promptId = prompt?.id;

  // Remember where focus was before the first prompt, and give it back after the last.
  useEffect(() => {
    if (promptId && !restoreRef.current && document.activeElement instanceof HTMLElement) {
      restoreRef.current = document.activeElement;
    }
    if (!promptId && restoreRef.current) {
      const target = restoreRef.current;
      restoreRef.current = null;
      if (target.isConnected) target.focus();
    }
  }, [promptId]);

  if (!prompt) return null;
  return <PromptDialog key={prompt.id} {...props} prompt={prompt} waiting={props.queue.length - 1} />;
}

interface PromptDialogProps extends PermissionPromptProps {
  prompt: PermissionPrompt;
  waiting: number;
}

function PromptDialog({
  t,
  client,
  prompt,
  waiting,
  device,
  pinSet,
  onAnswered,
  clock = Date.now,
}: PromptDialogProps) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const denyRef = useRef<HTMLButtonElement | null>(null);
  const pinRef = useRef<HTMLInputElement | null>(null);
  const busyRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pin, setPin] = useState('');
  const [now, setNow] = useState(clock);
  const [announcement, setAnnouncement] = useState('');
  const announcedRef = useRef<number | null>(bucket(secondsLeft(prompt.deadline, clock())));

  const highRisk = needsDevicePin(prompt.risk);
  const pinNeeded = device && highRisk;
  /** A paired device with no PIN set cannot allow this at all: it is not offered. */
  const canAllow = !(pinNeeded && pinSet === false);
  const remember = device ? null : rememberLabel(t, prompt.rememberTtlS);
  const offline = client === null;

  // Focus on Deny, or on the dialog itself while nothing in it can be pressed.
  useEffect(() => {
    if (denyRef.current && !denyRef.current.disabled) denyRef.current.focus();
    else dialogRef.current?.focus();
  }, []);

  // One tick a second for the countdown; the screen reader hears it at 30 and 10 seconds only.
  useEffect(() => {
    if (prompt.deadline === null) return;
    const timer = window.setInterval(() => {
      const current = clock();
      setNow(current);
      const at = bucket(secondsLeft(prompt.deadline, current));
      if (at !== null && at !== announcedRef.current) {
        announcedRef.current = at;
        setAnnouncement(fill(t('perm_countdown_announce'), secondsLeft(prompt.deadline, current) ?? at));
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [prompt.deadline, clock, t]);

  const answer = useCallback(
    async (decision: 'allow' | 'deny', remembered: boolean) => {
      if (busyRef.current) return;
      if (!client) {
        setError(t('perm_offline'));
        return;
      }
      const sendPin = decision === 'allow' && pinNeeded;
      if (sendPin && !pin.trim()) {
        setError(t('perm_pin_missing'));
        pinRef.current?.focus();
        return;
      }
      busyRef.current = true;
      setBusy(true);
      setError('');
      // The buttons are disabled while the answer travels; keep focus inside the dialog meanwhile.
      dialogRef.current?.focus();
      try {
        await api.permissionReply(
          client,
          prompt.id,
          decision,
          remembered && !device,
          sendPin ? pin.trim() : '',
        );
        onAnswered(prompt.id);
      } catch (e) {
        setError(refusalText(t, e, sendPin, t('perm_reply_failed')));
        if (sendPin) setPin('');
        busyRef.current = false;
        setBusy(false);
        window.setTimeout(() => (sendPin ? pinRef.current : denyRef.current)?.focus(), 0);
        return;
      }
      busyRef.current = false;
      setBusy(false);
    },
    [client, device, onAnswered, pin, pinNeeded, prompt.id, t],
  );

  // Escape denies and Tab stays inside, wherever focus is while the dialog is open. On the document,
  // so it also works while focus sits on the dialog itself or was lost to a disabled button.
  const answerRef = useRef(answer);
  useEffect(() => {
    answerRef.current = answer;
  });
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const dialog = dialogRef.current;
      if (!dialog) return;
      if (e.key === 'Escape') {
        e.preventDefault();
        void answerRef.current('deny', false);
        return;
      }
      if (e.key !== 'Tab') return;
      const focusable = [...dialog.querySelectorAll<HTMLElement>(FOCUSABLE)];
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) {
        e.preventDefault();
        dialog.focus();
        return;
      }
      const active = document.activeElement;
      const onItem = active instanceof HTMLElement && focusable.includes(active);
      if (e.shiftKey && (active === first || !onItem)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (active === last || !onItem)) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, []);

  const action = auditActionLabel(t, prompt.action);
  const sentence = prompt.target
    ? fill(t('perm_sentence'), action, prompt.target)
    : fill(t('perm_sentence_no_target'), action);
  const riskKey = RISK_KEYS[prompt.risk];
  const left = secondsLeft(prompt.deadline, now);
  const disabled = busy || offline;

  return (
    <div className="perm-backdrop">
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="perm-title"
        aria-describedby="perm-sentence perm-risk"
        aria-busy={busy || undefined}
        tabIndex={-1}
        className="perm-dialog"
      >
        <h2 id="perm-title" className="perm-title">
          {t('perm_title')}
        </h2>
        <p id="perm-sentence" className="perm-sentence">
          {sentence}
        </p>
        <dl className="perm-facts">
          <div>
            <dt>{t('perm_area')}</dt>
            <dd>{auditToolLabel(t, prompt.tool)}</dd>
          </div>
          {prompt.risk && (
            <div id="perm-risk">
              <dt>{t('perm_risk')}</dt>
              <dd>
                <span className={`perm-risk perm-risk--${prompt.risk}`}>
                  {riskKey ? t(riskKey) : prompt.risk}
                </span>
              </dd>
            </div>
          )}
        </dl>
        {left !== null && (
          <p className="hint perm-countdown">
            {left > 0 ? fill(t('perm_countdown'), left) : t('perm_countdown_over')}
          </p>
        )}
        <p className="sr-only" aria-live="polite">
          {announcement}
        </p>

        <details className="perm-technical">
          <summary>{t('perm_technical')}</summary>
          <dl className="perm-facts perm-facts--technical">
            <div>
              <dt>{t('perm_tool')}</dt>
              <dd className="mono">{prompt.tool}</dd>
            </div>
            <div>
              <dt>{t('perm_action')}</dt>
              <dd className="mono">{prompt.action}</dd>
            </div>
            {prompt.agent && (
              <div>
                <dt>{t('perm_agent')}</dt>
                <dd className="mono">{prompt.agent}</dd>
              </div>
            )}
          </dl>
        </details>

        {device && <p className="hint">{t('perm_device_note')}</p>}
        {pinNeeded && canAllow && (
          <div className="field">
            <label htmlFor="perm-pin" className="label">
              {t('perm_pin')}
            </label>
            <input
              ref={pinRef}
              id="perm-pin"
              className="input input--pin"
              type="password"
              inputMode="numeric"
              autoComplete="off"
              value={pin}
              disabled={disabled}
              onChange={(e) => setPin(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  void answer('allow', false);
                }
              }}
              aria-describedby="perm-pin-hint"
            />
            <p id="perm-pin-hint" className="hint">
              {t('perm_pin_hint')}
            </p>
          </div>
        )}
        {pinNeeded && !canAllow && <p className="hint perm-only-here">{t('perm_only_at_computer')}</p>}

        {offline && (
          <p role="status" className="alert">
            {t('perm_offline')}
          </p>
        )}
        {error && !(offline && error === t('perm_offline')) && (
          <p role="alert" className="alert">
            {error}
          </p>
        )}

        <div className={canAllow ? 'perm-actions' : 'perm-actions perm-actions--single'}>
          <button
            ref={denyRef}
            type="button"
            className="btn btn--quiet"
            disabled={disabled}
            onClick={() => void answer('deny', false)}
          >
            {t('perm_deny')}
          </button>
          {canAllow && (
            <button
              type="button"
              className={highRisk ? 'btn btn--risky' : 'btn'}
              disabled={disabled}
              onClick={() => void answer('allow', false)}
            >
              {t('perm_allow')}
            </button>
          )}
        </div>
        {remember && (
          <div className="perm-remember">
            <button
              type="button"
              className="btn btn--quiet btn--sm"
              disabled={disabled}
              onClick={() => void answer('allow', true)}
            >
              {remember}
            </button>
          </div>
        )}
        {waiting > 0 && <p className="hint perm-more">{fill(t('perm_more'), waiting)}</p>}
      </div>
    </div>
  );
}

/**
 * How a question this dashboard showed ended without its answer. The live region is always in the
 * page, so the sentence is announced when it appears; it is empty and invisible otherwise.
 */
export function PermissionNote({ t, note }: { t: T; note: DecidedNote | null }) {
  const text = note === 'timeout' ? t('perm_timed_out') : note === 'elsewhere' ? t('perm_decided_elsewhere') : '';
  return (
    <p role="status" className={text ? 'perm-note' : 'perm-note perm-note--empty'}>
      {text}
    </p>
  );
}
