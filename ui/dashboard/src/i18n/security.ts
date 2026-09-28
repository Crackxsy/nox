/**
 * PIN refusals as sentences. The core sends a stable reason (`details.reason`); this is the one
 * place that turns it into words, so the Status page and the Settings page say the same thing for
 * the same refusal - including how many attempts are left and until when a lockout runs.
 */

import { formatTimestamp } from '../../../shared/format';
import type { PinRefusal } from '../model/security';
import { type Lang, type T, fill } from './core';

export function pinRefusalText(t: T, lang: Lang, refusal: PinRefusal): string {
  switch (refusal.reason) {
    case 'pin_required':
      return t('pin_refusal_required');
    case 'pin_wrong':
      return refusal.remainingAttempts === null
        ? t('pin_refusal_wrong_plain')
        : fill(t('pin_refusal_wrong'), refusal.remainingAttempts);
    case 'locked':
      return refusal.lockedUntil === null
        ? t('pin_refusal_locked_plain')
        : fill(t('pin_refusal_locked'), formatTimestamp(refusal.lockedUntil, lang));
    case 'too_short':
      return fill(t('pin_refusal_too_short'), refusal.minLength ?? 6);
    case 'too_long':
      return fill(t('pin_refusal_too_long'), 64);
    case 'not_set':
      return t('pin_refusal_not_set');
    case 'invalid_entry':
      return t('pin_refusal_invalid_entry');
    case 'store_unavailable':
      return t('pin_refusal_store_unavailable');
    case 'backend_missing':
      return t('pin_refusal_backend_missing');
  }
}
