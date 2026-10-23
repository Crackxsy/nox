/**
 * "Darf Nox …?": one permission component in three places (Spec UX-Neubau §5) - inline in the
 * conversation, under "Wartet auf dich" on Heute, and as a sheet on every other page.
 *
 * The core asks every confirming client at once (`security.permission_requested`); the first answer
 * wins, and `security.permission_decided` takes the question away everywhere. A dashboard says how
 * a question it showed ended (`PermissionNote`): allowed, denied, nobody answered in time, decided
 * on another device, or cancelled by Stopp. A dashboard that (re)connects asks for the questions
 * still open (`security.permission.pending`).
 *
 * What it says: the heading is a yes/no question in everyday words ("Darf Nox ein Programm
 * starten?"), then one sentence with the target, the risk as a word and the time left in words.
 * Identifiers sit behind "Technische Angaben".
 *
 * Answers: Erlauben (the primary button - but not at high or critical risk, where it is outlined
 * like the rest), Ablehnen, and on the machine Nox runs on a quiet "Für 8 Stunden erlauben" (as long
 * as the core keeps such a grant - never "always"). A paired device answers once, and allows at
 * high or critical risk only with the security PIN; with no PIN set it says where this can be
 * allowed instead, and Ablehnen still works.
 *
 * Accessibility: the sheet is an `alertdialog` with a label and a description, focus on Ablehnen
 * (the answer that can never do harm), Tab kept inside, the page behind made `inert` by the app,
 * Escape denies (once, also while a reply is in flight), focus goes back where it was afterwards.
 * Inline, it is a labelled region and nothing is trapped. The countdown is announced at 30 and 10
 * seconds only.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { refusalText } from './hooks';
import { auditActionLabel, auditToolLabel, fill, type Key, rememberLabel, type T } from './i18n';
import { type IpcClient, api } from './ipc';
import { type PermissionPrompt, needsDevicePin, secondsLeft } from './model';
import { StopButton } from './shell/stop';

export interface PermissionPromptProps {
  t: T;
  client: IpcClient | null;
  queue: PermissionPrompt[];
  /** A paired device: no remembered grant, and the PIN at high or critical risk. */
  device: boolean;
  /** On a paired device: whether a security PIN is set; null while not known. */
  pinSet: boolean | null;
  /** Nox controls mouse and keyboard on a paired PC: allowing needs the PIN everywhere, and the
   * browser on that PC cannot allow at all (the core decides; `nox.node.world_routes`). */
  nodeControl?: boolean;
  /** Called once this dashboard answered (the core's event removes it too). */
  onAnswered: (id: string, decision: 'allow' | 'deny') => void;
  /** Injected in tests. */
  clock?: () => number;
  /** How long "Erlauben" waits after a question appears; `ARM_DELAY_MS` unless a test says 0. */
  armDelayMs?: number;
}

const RISK_KEYS: Record<string, Key> = {
  read: 'perm_risk_read',
  low: 'perm_risk_low',
  medium: 'perm_risk_medium',
  high: 'perm_risk_high',
  critical: 'perm_risk_critical',
};

/** The question in everyday words, by action; anything else asks "Darf Nox das?". */
const QUESTION_KEYS: Record<string, Key> = {
  process_start: 'ux_perm_q_process_start',
  process_stop: 'ux_perm_q_process_stop',
  delete: 'ux_perm_q_delete',
  write: 'ux_perm_q_write',
  move: 'ux_perm_q_move',
  read: 'ux_perm_q_read',
  type_text: 'ux_perm_q_input',
  input_send: 'ux_perm_q_input',
  screen_capture: 'ux_perm_q_screen',
  software_install: 'ux_perm_q_install',
  settings: 'ux_perm_q_settings',
  set: 'ux_perm_q_settings',
  request: 'ux_perm_q_request',
};

export function questionFor(t: T, action: string): string {
  const key = QUESTION_KEYS[action];
  return key ? t(key) : t('perm_title');
}

/** Seconds left at which the countdown is said out loud, once each. */
const ANNOUNCE_AT = [30, 10] as const;

function bucket(left: number | null): number | null {
  if (left === null || left <= 0) return null;
  let found: number | null = null;
  for (const at of ANNOUNCE_AT) if (left <= at) found = at;
  return found;
}

const FOCUSABLE = 'button:not(:disabled), input:not(:disabled), summary, a[href]';

/* ------------------------------------------------------------------ body - */

/**
 * How long a new question's "Erlauben" buttons stay disabled. After one answer the next question
 * appears with "Erlauben" in the same spot: a double click must not allow it unseen.
 */
export const ARM_DELAY_MS = 500;

interface PromptBodyProps extends Omit<PermissionPromptProps, 'queue'> {
  prompt: PermissionPrompt;
  waiting: number;
  /** id prefix, so the dialog and an inline card can both be on a page. */
  idBase: string;
  /** The heading level: 2 in the sheet, 3 inline under a section heading. */
  level: 2 | 3;
  /** Focus Ablehnen on mount (the sheet does; an inline card must not steal focus). */
  takeFocus: boolean;
  /** Where focus should rest while a reply travels; the sheet passes itself. */
  holdFocus?: () => void;
  answerRef?: React.MutableRefObject<((d: 'allow' | 'deny', r: boolean) => Promise<void>) | null>;
}

function PromptBody({
  t,
  client,
  prompt,
  waiting,
  device,
  pinSet,
  nodeControl = false,
  onAnswered,
  clock = Date.now,
  idBase,
  level,
  takeFocus,
  holdFocus,
  answerRef,
  armDelayMs = ARM_DELAY_MS,
}: PromptBodyProps) {
  const denyRef = useRef<HTMLButtonElement | null>(null);
  const pinRef = useRef<HTMLInputElement | null>(null);
  const busyRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pin, setPin] = useState('');
  const [now, setNow] = useState(clock);
  const [announcement, setAnnouncement] = useState('');
  const [armed, setArmed] = useState(armDelayMs <= 0);
  const announcedRef = useRef<number | null>(bucket(secondsLeft(prompt.deadline, clock())));

  const highRisk = needsDevicePin(prompt.risk);
  const pinNeeded = (device && highRisk) || nodeControl;
  /** A paired device with no PIN set cannot allow this at all: it is not offered. */
  const canAllow = !(pinNeeded && pinSet === false);
  const remember = device ? null : rememberLabel(t, prompt.rememberTtlS);
  const offline = client === null;

  useEffect(() => {
    if (!takeFocus) return;
    if (denyRef.current && !denyRef.current.disabled) denyRef.current.focus();
    else holdFocus?.();
  }, [takeFocus, holdFocus]);

  // Allowing waits a moment after the question appears (Ablehnen never waits: it is the safe answer).
  useEffect(() => {
    if (armed) return;
    const timer = window.setTimeout(() => setArmed(true), armDelayMs);
    return () => window.clearTimeout(timer);
  }, [armed, armDelayMs]);

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
      if (decision === 'allow' && !armed) return;
      const sendPin = decision === 'allow' && pinNeeded;
      if (sendPin && !pin.trim()) {
        setError(t('perm_pin_missing'));
        pinRef.current?.focus();
        return;
      }
      busyRef.current = true;
      setBusy(true);
      setError('');
      // The buttons are disabled while the answer travels; keep focus where it can stay.
      holdFocus?.();
      try {
        await api.permissionReply(client, prompt.id, decision, remembered && !device, sendPin ? pin.trim() : '');
        onAnswered(prompt.id, decision);
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
    [armed, client, device, holdFocus, onAnswered, pin, pinNeeded, prompt.id, t],
  );

  useEffect(() => {
    if (answerRef) answerRef.current = answer;
  });

  const action = auditActionLabel(t, prompt.action);
  const sentence = prompt.target
    ? fill(t('perm_sentence'), action, prompt.target)
    : fill(t('perm_sentence_no_target'), action);
  const riskKey = RISK_KEYS[prompt.risk];
  const left = secondsLeft(prompt.deadline, now);
  const disabled = busy || offline;
  const Heading = level === 2 ? 'h2' : 'h3';

  return (
    <>
      <div className="ask-head">
        <span className="lamp lamp--sm" aria-hidden="true" />
        <Heading id={`${idBase}-title`} className="ask-title">
          {questionFor(t, prompt.action)}
        </Heading>
      </div>
      <p id={`${idBase}-sentence`} className="ask-what">
        {sentence}
      </p>
      <p id={`${idBase}-risk`} className="ask-when">
        {prompt.risk && (
          <span className={`badge badge--risk badge--risk-${prompt.risk}`}>
            {fill(t('ux_perm_risk'), riskKey ? t(riskKey) : prompt.risk)}
          </span>
        )}{' '}
        {left !== null && (
          <span className="ask-left">{left > 0 ? fill(t('perm_countdown'), left) : t('perm_countdown_over')}</span>
        )}
      </p>
      <p className="sr-only" aria-live="polite">
        {announcement}
      </p>

      {prompt.voiceSatellite && (
        <p className="hint" data-testid="perm-voice-satellite">
          {fill(t('perm_voice_satellite'), prompt.voiceSatellite)}
        </p>
      )}
      {device && <p className="hint">{t('perm_device_note')}</p>}
      {pinNeeded && canAllow && (
        <div className="field">
          <label htmlFor={`${idBase}-pin`} className="label">
            {t('perm_pin')}
          </label>
          <input
            ref={pinRef}
            id={`${idBase}-pin`}
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
            aria-describedby={`${idBase}-pin-hint`}
          />
          <p id={`${idBase}-pin-hint`} className="hint">
            {t(nodeControl ? 'perm_node_control_pin_hint' : 'perm_pin_hint')}
          </p>
        </div>
      )}
      {pinNeeded && !canAllow && <p className="hint perm-only-here">{t('perm_only_at_computer')}</p>}
      {device && nodeControl && canAllow && <p className="hint">{t('perm_node_control_device')}</p>}

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

      <div className="btns ask-btns">
        {canAllow && (
          <button
            type="button"
            className={highRisk ? 'btn btn--risky' : 'btn btn--primary'}
            disabled={disabled || !armed}
            onClick={() => void answer('allow', false)}
          >
            {t('perm_allow')}
          </button>
        )}
        <button
          ref={denyRef}
          type="button"
          className="btn"
          disabled={disabled}
          onClick={() => void answer('deny', false)}
        >
          {t('perm_deny')}
        </button>
        {remember && (
          <button
            type="button"
            className="btn btn--plain"
            disabled={disabled || !armed}
            onClick={() => void answer('allow', true)}
          >
            {remember}
          </button>
        )}
      </div>

      <details className="tech">
        <summary>{t('perm_technical')}</summary>
        <dl className="tech-list">
          {prompt.agent && (
            <>
              <dt>{t('perm_agent')}</dt>
              <dd className="mono">{prompt.agent}</dd>
            </>
          )}
          <dt>{t('perm_area')}</dt>
          <dd>{auditToolLabel(t, prompt.tool)}</dd>
          <dt>{t('perm_tool')}</dt>
          <dd className="mono">{prompt.tool}</dd>
          <dt>{t('perm_action')}</dt>
          <dd className="mono">{prompt.action}</dd>
          {prompt.target && (
            <>
              <dt>{t('perm_target')}</dt>
              <dd>
                <code className="code">{prompt.target}</code>
              </dd>
            </>
          )}
        </dl>
      </details>
      {waiting > 0 && <p className="hint perm-more">{fill(t('perm_more'), waiting)}</p>}
    </>
  );
}

/* ----------------------------------------------------------------- sheet - */

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

function PromptDialog(props: PermissionPromptProps & { prompt: PermissionPrompt; waiting: number }) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const answerRef = useRef<((d: 'allow' | 'deny', r: boolean) => Promise<void>) | null>(null);
  const hold = useCallback(() => dialogRef.current?.focus(), []);

  // Escape denies and Tab stays inside, wherever focus is while the dialog is open. On the document,
  // so it also works while focus sits on the dialog itself or was lost to a disabled button.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const dialog = dialogRef.current;
      if (!dialog) return;
      if (e.key === 'Escape') {
        // Escape that already closed something else (a menu still open) is not an answer.
        if (e.defaultPrevented || document.querySelector('dialog[open]')) return;
        e.preventDefault();
        void answerRef.current?.('deny', false);
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

  return (
    <div className="perm-backdrop">
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="perm-title"
        aria-describedby="perm-sentence perm-risk"
        tabIndex={-1}
        className="perm-dialog ask"
      >
        <PromptBody {...props} idBase="perm" level={2} takeFocus holdFocus={hold} answerRef={answerRef} />
        {/* The page behind is inert; Stopp stays where it always is (Spec §5). */}
        <StopButton placement="corner" />
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- inline - */

/**
 * The first open question as a card in the page (Heute, the conversation). Nothing is trapped and
 * nothing is inert: the person reads it where it belongs and can still scroll past.
 */
export function PermissionInline(props: PermissionPromptProps & { idBase?: string }) {
  const prompt = props.queue[0];
  if (!prompt) return null;
  const idBase = props.idBase ?? 'perm-inline';
  return (
    <section
      key={prompt.id}
      className="ask"
      aria-labelledby={`${idBase}-title`}
      aria-describedby={`${idBase}-sentence ${idBase}-risk`}
    >
      <PromptBody
        {...props}
        prompt={prompt}
        waiting={props.queue.length - 1}
        idBase={idBase}
        level={3}
        takeFocus={false}
      />
    </section>
  );
}
