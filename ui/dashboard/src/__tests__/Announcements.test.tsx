/**
 * State that follows a prop, adjusted while rendering instead of from an effect.
 *
 * Pinned here: the personality box adopts what the core reports but never over unsaved edits, and
 * the history (formerly audit) page announces its count when the filter changes - not when a new
 * event arrives.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { translator } from '../i18n';
import type { AuditRow } from '../model';
import { HistoryPage } from '../pages/History';
import { PersonalityTile } from '../pages/settings/PersonalityTile';

const t = translator('de');

function row(seq: number, action: string): AuditRow {
  return {
    seq,
    actor: 'core',
    tool: 'files',
    action,
    target: '',
    decision: 'allow',
    result: 'ok',
    taskId: null,
    ts: '2026-10-01T12:00:00Z',
  };
}

describe('PersonalityTile', () => {
  const props = { t, client: null, failed: false, onSaved: () => {} };

  it('adopts each personality the core reports', () => {
    const { rerender } = render(
      <PersonalityTile {...props} personality={{ text: 'eins', path: 'p' }} />,
    );
    expect(screen.getByRole('textbox')).toHaveProperty('value', 'eins');
    rerender(<PersonalityTile {...props} personality={{ text: 'zwei', path: 'p' }} />);
    expect(screen.getByRole('textbox')).toHaveProperty('value', 'zwei');
  });

  it('keeps unsaved edits when the core reports a change', () => {
    const { rerender } = render(
      <PersonalityTile {...props} client={{} as never} personality={{ text: 'eins', path: 'p' }} />,
    );
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'mein Entwurf' } });
    rerender(
      <PersonalityTile {...props} client={{} as never} personality={{ text: 'zwei', path: 'p' }} />,
    );
    expect(screen.getByRole('textbox')).toHaveProperty('value', 'mein Entwurf');
  });
});

describe('HistoryPage announcement', () => {
  const live = () => screen.getByRole('status');

  it('announces the count when the filter changes, not when an event arrives', () => {
    const denied = { ...row(4, 'delete'), decision: 'deny', result: 'denied' };
    const { rerender } = render(<HistoryPage t={t} lang="de" rows={[row(1, 'read'), row(2, 'write')]} />);
    expect(live().textContent).toBe('2 Einträge');

    rerender(<HistoryPage t={t} lang="de" rows={[row(1, 'read'), row(2, 'write'), denied]} />);
    expect(live().textContent).toBe('2 Einträge');

    fireEvent.click(screen.getByRole('button', { name: t('ux_hist_denied') }));
    expect(live().textContent).toBe('1 Einträge');
    fireEvent.click(screen.getByRole('button', { name: t('ux_hist_allowed') }));
    expect(live().textContent).toBe('2 Einträge');
  });

  it('says what happened in words and keeps the identifiers under "Technische Angaben"', () => {
    render(<HistoryPage t={t} lang="de" rows={[{ ...row(7, 'delete'), target: 'Archiv/alt.zip', decision: 'deny', result: 'denied' }]} />);
    const item = screen.getByText('Archiv/alt.zip').closest('li')!;
    expect(item.textContent).toContain(t('ux_hist_outcome_denied'));
    const details = item.querySelector('details')!;
    expect(details.open).toBe(false);
    expect(details.textContent).toContain('delete');
  });
});
