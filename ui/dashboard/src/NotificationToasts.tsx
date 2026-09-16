/**
 * Global toast panel for `proactive.notification` events (ST-19/ST-08-01). Purely presentational:
 * `App.tsx` owns the list in `DashboardState.notifications` (via `reduceEvent`); this component
 * only renders it and lets the user dismiss one early (auto-dismiss after a delay otherwise).
 */

import { useEffect } from 'react';

import type { T } from './i18n';
import type { ProactiveNotification } from './model';

export interface NotificationToastsProps {
  t: T;
  notifications: ProactiveNotification[];
  onDismiss: (id: string) => void;
}

const AUTO_DISMISS_MS = 12000;

/** Priority decides the accent stripe only; the text is always shown verbatim. */
const TONE: Record<string, string> = {
  security: 'toast--danger',
  data_loss: 'toast--danger',
  resources: 'toast--warn',
  task_result: 'toast--ok',
};

export function NotificationToasts({ t, notifications, onDismiss }: NotificationToastsProps) {
  const shown = notifications.slice(0, 5);

  const shownIds = shown.map((n) => n.id).join(',');
  useEffect(() => {
    if (shown.length === 0) return;
    const timers = shown.map((n) => window.setTimeout(() => onDismiss(n.id), AUTO_DISMISS_MS));
    return () => timers.forEach((id) => window.clearTimeout(id));
    // Re-arm only when the visible toast set actually changes (`shownIds`), not on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownIds]);

  if (shown.length === 0) return null;

  return (
    <div role="region" aria-label={t('notif_title')} className="toasts">
      {shown.map((n) => (
        <div
          key={n.id}
          role={n.kind === 'urgent' ? 'alert' : 'status'}
          aria-live={n.kind === 'urgent' ? 'assertive' : 'polite'}
          className={`toast ${TONE[n.priority] ?? ''}`}
        >
          <span className="toast-text">{n.text}</span>
          <button
            type="button"
            onClick={() => onDismiss(n.id)}
            aria-label={t('notif_dismiss')}
            className="toast-close"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
