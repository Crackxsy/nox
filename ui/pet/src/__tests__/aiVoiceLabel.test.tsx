/**
 * The overlay's AI-voice label: shown on the OBS overlay with the configured text, never on the
 * desktop pet, and kept in step with the core state (`system.ai_voice_label`).
 */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { AiVoiceLabel } from '../AiVoiceLabel';
import {
  AI_VOICE_LABEL_PATH,
  INITIAL_STATE,
  aiVoiceLabelFromSnapshot,
  reduceEvent,
} from '../petState';

afterEach(() => cleanup());

describe('AiVoiceLabel', () => {
  it('shows the configured text on the overlay', () => {
    render(<AiVoiceLabel label="KI-Stimme" overlay />);
    const label = screen.getByTestId('ai-voice-label');
    expect(label.textContent).toBe('KI-Stimme');
    expect(label.className).toBe('pet-ai-voice');
  });

  it('is never rendered on the desktop pet', () => {
    const { container } = render(<AiVoiceLabel label="KI-Stimme" overlay={false} />);
    expect(container.innerHTML).toBe('');
  });

  it('renders nothing for an empty or blank label', () => {
    const { container } = render(<AiVoiceLabel label="   " overlay />);
    expect(container.innerHTML).toBe('');
  });
});

describe('the label in the pet state', () => {
  it('starts empty and follows state.changed', () => {
    expect(INITIAL_STATE.aiVoiceLabel).toBe('');
    const next = reduceEvent(INITIAL_STATE, 'state.changed', {
      path: AI_VOICE_LABEL_PATH,
      new: 'AI voice',
    });
    expect(next.aiVoiceLabel).toBe('AI voice');
    const cleared = reduceEvent(next, 'state.changed', { path: AI_VOICE_LABEL_PATH, new: null });
    expect(cleared.aiVoiceLabel).toBe('');
  });

  it('is read from the public state.get snapshot', () => {
    expect(aiVoiceLabelFromSnapshot({ system: { ai_voice_label: 'KI-Stimme' } })).toBe('KI-Stimme');
    expect(aiVoiceLabelFromSnapshot({ system: { ai_voice_label: '' } })).toBe('');
    // A quiet overlay snapshot (private/offline) or an older core: nothing to change.
    expect(aiVoiceLabelFromSnapshot({ privacy: { mode: 'private' } })).toBeNull();
    expect(aiVoiceLabelFromSnapshot('nonsense')).toBeNull();
  });
});
