/**
 * Capture indicator (PRD §20, NFR-8): mic / screen / camera / cloud dots. It has no prop that can
 * hide it and is always mounted on the desktop pet. The only place it is not rendered is the OBS
 * overlay (`?overlay=1`), because the PRD forbids the indicator in the public stream output.
 * Each dot carries a letter and a shape change so the state is never colour-only (D238).
 *
 * #25: the colours are the shared tokens (`ui/shared/tokens.css`), so the chip follows the OS
 * between light and dark like the dashboard does instead of being a fixed dark plate. `--warn`,
 * `--danger` and `--blue` already carry their meaning in both themes; the styling itself lives in
 * `styles.css` (`.pet-chip` / `.pet-capture*`) and each dot passes its own colour in as `--dot`.
 */

import type { CaptureFlags } from './petState';

const ITEMS: { key: keyof CaptureFlags; letter: string; label: string; token: string }[] = [
  { key: 'microphone', letter: 'M', label: 'Mikrofon / microphone', token: 'var(--warn)' },
  { key: 'screen', letter: 'S', label: 'Bildschirm / screen', token: 'var(--warn)' },
  { key: 'camera', letter: 'C', label: 'Kamera / camera', token: 'var(--danger)' },
  { key: 'cloud', letter: 'W', label: 'Cloud-Anfrage / cloud request', token: 'var(--blue)' },
];

export interface CaptureIndicatorProps {
  capture: CaptureFlags;
  connected: boolean;
  privacyMode: string;
  muted: boolean;
}

export function CaptureIndicator({ capture, connected, privacyMode, muted }: CaptureIndicatorProps) {
  const active = ITEMS.filter((i) => capture[i.key]).map((i) => i.label);
  const summary = !connected
    ? 'Nox offline – Aufnahmestatus unbekannt / capture state unknown'
    : active.length
      ? `Aktiv / active: ${active.join(', ')}`
      : 'Keine Aufnahme / no capture';
  return (
    <div
      role="status"
      aria-live="polite"
      aria-label={summary}
      title={`${summary} · privacy: ${privacyMode}${muted ? ' · muted' : ''}`}
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
      {!connected && <span className="pet-capture-offline">offline</span>}
    </div>
  );
}
