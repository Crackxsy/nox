/**
 * The pet window after a core restart: it says what happened and how to get back, in the window's
 * language, instead of "Authentifizierung abgelehnt" forever.
 */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { ConnectionNote } from '../ConnectionNote';
import { petTranslator } from '../strings';

afterEach(() => cleanup());

describe('ConnectionNote', () => {
  it('explains a core restart and how to reopen, in German', () => {
    const t = petTranslator('de');
    render(<ConnectionNote t={t} hasToken status="session_expired" />);
    const note = screen.getByRole('alert');
    expect(note.textContent).toContain('neu gestartet');
    expect(note.textContent).toContain('Infobereich');
  });

  it('explains a core restart in English too', () => {
    render(<ConnectionNote t={petTranslator('en')} hasToken status="session_expired" />);
    expect(screen.getByRole('alert').textContent).toContain('restarted');
  });

  it('keeps the refusal reason for a token that was never accepted', () => {
    const t = petTranslator('de');
    render(<ConnectionNote t={t} hasToken status="auth_failed" detail="client version 9.0 not compatible" />);
    expect(screen.getByRole('alert').textContent).toBe(
      `${t('auth_denied')}: client version 9.0 not compatible`,
    );
  });

  it('says nothing while simply connecting or offline', () => {
    const t = petTranslator('de');
    const { container } = render(<ConnectionNote t={t} hasToken status="offline" />);
    expect(container.textContent).toBe('');
  });

  it('says how to open the window without a token', () => {
    const t = petTranslator('de');
    render(<ConnectionNote t={t} hasToken={false} status="offline" />);
    expect(screen.getByRole('alert').textContent).toBe(t('no_token'));
  });
});
