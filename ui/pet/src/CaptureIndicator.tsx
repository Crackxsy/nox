/**
 * Capture indicator (PRD §20, NFR-8): mic / screen / camera / cloud. It has no prop that can hide
 * it and is always mounted on the desktop pet. The only place it is not rendered is the OBS overlay
 * (`?overlay=1`), because the PRD forbids the indicator in the public stream output.
 *
 * Each dot carries a letter *and* a shape change, so the state is never colour-only (D238). A
 * letter on its own is not an explanation, so whichever capture is running is also spelled out next
 * to the dots ("Mikrofon") instead of being hidden in a tooltip — and the tooltip is in one
 * language now, not two.
 *
 * Colours are the shared tokens (`ui/shared/tokens.css`), so the chip follows the OS between light
 * and dark like the dashboard does instead of being a fixed dark plate.
 */

import type { CaptureFlags } from './petState';
import type { PetKey, PetT } from './strings';

const ITEMS: { key: keyof CaptureFlags; letter: string; label: PetKey; token: string }[] = [
  { key: 'microphone', letter: 'M', label: 'capture_microphone', token: 'var(--warn)' },
  { key: 'screen', letter: 'S', label: 'capture_screen', token: 'var(--warn)' },
  { key: 'camera', letter: 'C', label: 'capture_camera', token: 'var(--danger)' },
  { key: 'cloud', letter: 'W', label: 'capture_cloud', token: 'var(--blue)' },
];

export interface CaptureIndicatorProps {
  t: PetT;
  capture: CaptureFlags;
  connected: boolean;
  privacyMode: string;
  muted: boolean;
}

export function CaptureIndicator({
  t,
  capture,
  connected,
  privacyMode,
  muted,
}: CaptureIndicatorProps) {
  const active = ITEMS.filter((i) => capture[i.key]).map((i) => t(i.label));
  const summary = !connected
    ? t('capture_unknown')
    : active.length > 0
      ? `${active.join(', ')} ${t('capture_active').toLowerCase()}`
      : t('capture_none');
  const title = `${summary} · ${t('privacy')}: ${privacyMode}${muted ? ` · ${t('muted')}` : ''}`;

  return (
    <div
      role="status"
      aria-live="polite"
      aria-label={summary}
      title={title}
      className="pet-chip pet-capture"
    >
      {ITEMS.map((i) => {
        const on = connected && capture[i.key];
        return (
          <span
            key={i.key}
            aria-hidden="true"
            className="pet-capture-dot"
            data-on={on ? 'true' : 'false'}
            style={{ ['--dot' as string]: i.token }}
          >
            {i.letter}
          </span>
        );
      })}
      {connected && active.length > 0 && (
        <span aria-hidden="true" className="pet-capture-word">
          {active[0]}
        </span>
      )}
      {!connected && (
        <span aria-hidden="true" className="pet-capture-offline">
          {t('offline')}
        </span>
      )}
    </div>
  );
}
