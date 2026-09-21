/**
 * Proactive notifications (Spec v0.5 EPIC-19/ST-08-01): the toast panel's rows.
 *
 * Nothing here is a durable log — `nox.proactive.store` is. Dismissing a toast is local only.
 * Toasts that report a security event or possible data loss are *not* auto-dismissed: a message
 * the user did not see must still be there when they look.
 */

import { bool, str } from '../../../shared/guards';

export interface ProactiveNotification {
  id: string;
  /** "urgent" | "proactive" */
  kind: string;
  priority: string;
  text: string;
  /** "speech" | "toast" | "speech+toast" */
  channel: string;
  spoken: boolean;
  announced: boolean;
  ts: string;
}

/** Keep the live toast list bounded; nothing here is a durable log. */
export const NOTIFICATION_LIMIT = 20;

/** Priorities that stay on screen until the user closes them. */
const STICKY_PRIORITIES: ReadonlySet<string> = new Set(['security', 'data_loss', 'urgent']);

export function isSticky(n: ProactiveNotification): boolean {
  return STICKY_PRIORITIES.has(n.priority) || STICKY_PRIORITIES.has(n.kind);
}

/**
 * One `proactive.notification` event. `seq` comes from `DashboardState`, so the reducer stays pure
 * and two tests cannot share a hidden counter.
 */
export function notificationRow(
  payload: Record<string, unknown>,
  ts: string,
  seq: number,
): ProactiveNotification | null {
  const text = str(payload.text);
  if (!text) return null; // suppressed/empty notifications never reach the dashboard as toasts
  const id = str(payload.id) || `${ts || 'n'}-${seq}`;
  return {
    id,
    kind: str(payload.kind),
    priority: str(payload.priority),
    text,
    channel: str(payload.channel),
    spoken: bool(payload.spoken),
    announced: bool(payload.announced),
    ts,
  };
}

/** Local-only dismissal. `null` means "nothing changed", so the caller can keep state identity. */
export function dismiss(
  notifications: ProactiveNotification[],
  id: string,
): ProactiveNotification[] | null {
  const next = notifications.filter((n) => n.id !== id);
  return next.length === notifications.length ? null : next;
}
