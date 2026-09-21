/**
 * Global toast panel for `proactive.notification` events (ST-19/ST-08-01). Purely presentational:
 * `App.tsx` owns the list in `DashboardState.notifications` (via `reduceEvent`); this component
 * only renders it and lets the user dismiss one.
 *
 * A toast about a security event or possible data loss is **not** auto-dismissed. There is no
 * notification history anywhere in this UI, so a toast that disappears on its own is a message the
 * user may never see — acceptable for "task finished", not for "something was blocked".
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import type { T } from './i18n';
import { type ProactiveNotification, isSticky } from './model';

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

/** Matches the `toast-out` animation in `styles.css`; the toast is removed when it has played. */
const LEAVE_MS = 180;

export function NotificationToasts({ t, notifications, onDismiss }: NotificationToastsProps) {
  const shown = notifications.slice(0, 5);
  const shownIds = shown.map((n) => n.id).join(',');
  const dismissRef = useRef(onDismiss);
  dismissRef.current = onDismiss;
  /** Toasts that are playing their exit animation and will be gone a moment later. */
  const [leaving, setLeaving] = useState<readonly string[]>([]);

  const dismiss = useCallback((id: string) => {
    setLeaving((list) => (list.includes(id) ? list : [...list, id]));
    window.setTimeout(() => {
      dismissRef.current(id);
      setLeaving((list) => list.filter((x) => x !== id));
    }, LEAVE_MS);
  }, []);

  useEffect(() => {
    const expiring = shown.filter((n) => !isSticky(n));
    if (expiring.length === 0) return;
    const timers = expiring.map((n) => window.setTimeout(() => dismiss(n.id), AUTO_DISMISS_MS));
    return () => timers.forEach((id) => window.clearTimeout(id));
    // Re-arm only when the visible toast set actually changes (`shownIds`), not on every render;
    // `shown` is derived from it and `onDismiss` is read through a ref for the same reason.
  }, [shownIds, shown, dismiss]);

  if (shown.length === 0) return null;

  return (
    <div role="region" aria-label={t('notif_title')} className="toasts">
      {shown.map((n) => (
        <div
          key={n.id}
          role={n.kind === 'urgent' ? 'alert' : 'status'}
          aria-live={n.kind === 'urgent' ? 'assertive' : 'polite'}
          data-leaving={leaving.includes(n.id) ? 'true' : undefined}
          className={`toast ${TONE[n.priority] ?? ''}`}
        >
          <span className="toast-text">{n.text}</span>
          <button
            type="button"
            onClick={() => dismiss(n.id)}
            aria-label={t('notif_dismiss')}
            className="toast-close"
          >
            <span aria-hidden="true">×</span>
          </button>
        </div>
      ))}
    </div>
  );
}
