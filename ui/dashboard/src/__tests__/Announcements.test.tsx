/**
 * State that follows a prop, adjusted while rendering instead of from an effect.
 *
 * Pinned here: the personality box adopts what the core reports but never over unsaved edits, and
 * the audit page announces its count when the filter changes - not when a new event arrives.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { translator } from '../i18n';
import type { AuditRow } from '../model';
import { AuditPage } from '../pages/Audit';
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

describe('AuditPage announcement', () => {
  const live = () => screen.getByRole('status');

  it('announces the count when the filter changes, not when an event arrives', () => {
    const { rerender } = render(<AuditPage t={t} lang="de" rows={[row(1, 'read'), row(2, 'write')]} />);
    expect(live().textContent).toBe('2 Einträge');

    rerender(<AuditPage t={t} lang="de" rows={[row(1, 'read'), row(2, 'write'), row(3, 'read')]} />);
    expect(live().textContent).toBe('2 Einträge');

    fireEvent.change(screen.getByLabelText(t('audit_filter')), { target: { value: 'read' } });
    expect(live().textContent).toBe('2 Einträge');
    fireEvent.change(screen.getByLabelText(t('audit_filter')), { target: { value: 'write' } });
    expect(live().textContent).toBe('1 Einträge');
  });
});
