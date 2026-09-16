/**
 * Capture indicator (PRD §20, NFR-8): mic / screen / camera / cloud dots. It has no prop that can
 * hide it and is always mounted on the desktop pet. The only place it is not rendered is the OBS
 * overlay (`?overlay=1`), because the PRD forbids the indicator in the public stream output.
 * Each dot carries a letter and a shape change so the state is never colour-only (D238).
 */

import type { CaptureFlags } from './petState';

export interface CaptureIndicatorProps {
  capture: CaptureFlags;
  connected: boolean;
  privacyMode: string;
  muted: boolean;
}

const ITEMS: { key: keyof CaptureFlags; letter: string; label: string; colour: string }[] = [
  { key: 'microphone', letter: 'M', label: 'Mikrofon / microphone', colour: '#e0a23a' },
  { key: 'screen', letter: 'S', label: 'Bildschirm / screen', colour: '#e0a23a' },
  { key: 'camera', letter: 'C', label: 'Kamera / camera', colour: '#e05a5a' },
  { key: 'cloud', letter: 'W', label: 'Cloud-Anfrage / cloud request', colour: '#4ea1e0' },
];

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
      style={{
        position: 'absolute',
        top: 6,
        left: 6,
        display: 'flex',
        gap: 4,
        padding: '3px 5px',
        borderRadius: 10,
        background: 'rgba(11,11,18,0.8)',
        fontSize: 10,
        lineHeight: '14px',
        fontWeight: 600,
        letterSpacing: 0.5,
      }}
    >
      {ITEMS.map((i) => {
        const on = connected && capture[i.key];
        return (
          <span
            key={i.key}
            aria-hidden="true"
            style={{
              width: 14,
              height: 14,
              borderRadius: on ? 3 : 7,
              background: on ? i.colour : 'transparent',
              border: `1.5px solid ${connected ? (on ? i.colour : '#55555f') : '#3a3a46'}`,
              color: on ? '#0b0b12' : '#6b6b76',
              textAlign: 'center',
              boxSizing: 'border-box',
            }}
          >
            {i.letter}
          </span>
        );
      })}
      {!connected && <span style={{ color: '#9a9aa8', paddingLeft: 2 }}>offline</span>}
    </div>
  );
}
