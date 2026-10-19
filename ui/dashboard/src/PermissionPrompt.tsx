/**
 * "Nox wants to do something": the permission prompt, on every dashboard a person looks at.
 *
 * The core asks every confirming client at once (`security.permission_requested`); the first answer
 * wins, and `security.permission_decided` takes the prompt away everywhere - so a phone that was
 * slower than the PC simply sees it disappear, with a one-line note.
 *
 * Accessibility: an `alertdialog` with a real label and description, focus moved to "Deny" (the
 * answer that can never do harm), Escape denies, Tab stays inside the dialog while it is open,
 * and focus goes back where it was afterwards.
 *
 * A paired device never offers "Always allow": its answer counts once (the core enforces that too,
 * `CoreHandlers.permission_reply`), so a lost phone cannot widen what Nox may do later.
 */

import { useEffect, useRef, useState } from 'react';

import { auditActionLabel, auditToolLabel, fill, type Key, type T } from './i18n';
import { type IpcClient, api } from './ipc';
import type { PermissionPrompt } from './model';
import { Detail } from './ui';

export interface PermissionPromptProps {
  t: T;
  client: IpcClient | null;
  queue: PermissionPrompt[];
  /** A paired device: no "always", and a note that an answer counts once. */
  device: boolean;
  /** Called with the prompt id once this dashboard answered (the core's event removes it too). */
  onAnswered: (id: string) => void;
  /** Set when the prompt shown last was decided elsewhere. */
  decidedElsewhere: boolean;
}

const RISK_KEYS: Record<string, Key> = {
  read: 'perm_risk_read',
  low: 'perm_risk_low',
  medium: 'perm_risk_medium',
  high: 'perm_risk_high',
  critical: 'perm_risk_critical',
};

export function PermissionPromptDialog({
  t,
  client,
  queue,
  device,
  onAnswered,
  decidedElsewhere,
}: PermissionPromptProps) {
  const prompt = queue[0];
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const denyRef = useRef<HTMLButtonElement | null>(null);
  const restoreRef = useRef<HTMLElement | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const promptId = prompt?.id;
  useEffect(() => {
    if (!promptId) return;
    if (!restoreRef.current && document.activeElement instanceof HTMLElement) {
      restoreRef.current = document.activeElement;
    }
    denyRef.current?.focus();
    return undefined;
  }, [promptId]);

  useEffect(() => {
    if (prompt) return;
    restoreRef.current?.focus();
    restoreRef.current = null;
  }, [prompt]);

  // Escape denies and Tab stays inside, wherever focus is while the dialog is open. On the document
  // rather than the dialog element, which is not itself an interactive control.
  useEffect(() => {
    if (!promptId) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        if (client) {
          void api
            .permissionReply(client, promptId, 'deny', false)
            .then(() => onAnswered(promptId))
            .catch(() => undefined);
        }
        return;
      }
      if (e.key !== 'Tab' || !dialogRef.current) return;
      const focusable = [...dialogRef.current.querySelectorAll<HTMLElement>('button:not(:disabled)')];
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      const inside = dialogRef.current.contains(document.activeElement);
      if (e.shiftKey && (document.activeElement === first || !inside)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (document.activeElement === last || !inside)) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [promptId, client, onAnswered]);

  if (!prompt) {
    return decidedElsewhere ? (
      <p role="status" className="perm-elsewhere">
        {t('perm_decided_elsewhere')}
      </p>
    ) : null;
  }

  const answer = async (decision: 'allow' | 'deny', remember: boolean) => {
    if (!client || busy) return;
    setBusy(true);
    setError('');
    try {
      await api.permissionReply(client, prompt.id, decision, remember && !device);
      onAnswered(prompt.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };


  const riskKey = RISK_KEYS[prompt.risk];
  const waiting = queue.length - 1;

  return (
    <div className="perm-backdrop">
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="perm-title"
        aria-describedby="perm-details"
        className="perm-dialog"
      >
        <h2 id="perm-title" className="perm-title">
          {t('perm_title')}
        </h2>
        <dl id="perm-details" className="perm-details">
          <div>
            <dt>{t('perm_tool')}</dt>
            <dd>
              <Detail label={auditToolLabel(t, prompt.tool)} detail={prompt.tool} />
            </dd>
          </div>
          <div>
            <dt>{t('perm_action')}</dt>
            <dd>
              <Detail label={auditActionLabel(t, prompt.action)} detail={prompt.action} />
            </dd>
          </div>
          {prompt.target && (
            <div>
              <dt>{t('perm_target')}</dt>
              <dd className="perm-target">{prompt.target}</dd>
            </div>
          )}
          {riskKey && (
            <div>
              <dt>{t('perm_risk')}</dt>
              <dd>
                <span className={`perm-risk perm-risk--${prompt.risk}`}>{t(riskKey)}</span>
              </dd>
            </div>
          )}
        </dl>
        {device && <p className="hint">{t('perm_device_note')}</p>}
        {error && (
          <p role="alert" className="alert">
            {error}
          </p>
        )}
        <div className="perm-actions">
          <button
            ref={denyRef}
            type="button"
            className="btn btn--quiet"
            disabled={busy || !client}
            onClick={() => void answer('deny', false)}
          >
            {t('perm_deny')}
          </button>
          {!device && (
            <button
              type="button"
              className="btn btn--quiet"
              disabled={busy || !client}
              onClick={() => void answer('allow', true)}
            >
              {t('perm_always')}
            </button>
          )}
          <button
            type="button"
            className="btn"
            disabled={busy || !client}
            onClick={() => void answer('allow', false)}
          >
            {t('perm_allow')}
          </button>
        </div>
        {waiting > 0 && <p className="hint perm-more">{fill(t('perm_more'), waiting)}</p>}
      </div>
    </div>
  );
}
