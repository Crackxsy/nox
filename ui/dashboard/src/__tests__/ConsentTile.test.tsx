/**
 * Recording consents: the list shows every stored field, a grant goes out trimmed and the list is
 * reloaded, an incomplete form cannot be sent, a withdrawal names the context, and a PIN refusal
 * gets the PIN message instead of the core's raw error.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { fill, translator } from '../i18n';
import { type IpcClient, IpcError } from '../ipc';
import { ConsentTile, parseConsents, validateConsent } from '../pages/settings/ConsentTile';

const t = translator('de');

interface WireConsent {
  id: string;
  context: string;
  who: string;
  wording: string;
  granted_at: string;
  recorded_by: string;
}

const MATH: WireConsent = {
  id: 'c1',
  context: 'lesson:math',
  who: 'Klasse 10b',
  wording: 'Wir sind einverstanden, dass die Stunde aufgenommen wird.',
  granted_at: '2026-10-01T08:15:00Z',
  recorded_by: 'dashboard (dashboard)',
};

function fakeClient(rows: WireConsent[], options: { failGrant?: IpcError } = {}) {
  const sent: [string, Record<string, unknown>][] = [];
  const client = {
    request: async (name: string, payload: Record<string, unknown> = {}) => {
      sent.push([name, payload]);
      if (name === 'consent.list') return { consents: rows.map((r) => ({ ...r })) };
      if (name === 'consent.grant') {
        if (options.failGrant) throw options.failGrant;
        const row: WireConsent = {
          id: `c${rows.length + 1}`,
          context: String(payload.context),
          who: String(payload.who),
          wording: String(payload.wording),
          granted_at: '2026-10-02T09:00:00Z',
          recorded_by: 'dashboard (dashboard)',
        };
        rows.push(row);
        return { ok: true, consent: row };
      }
      if (name === 'consent.revoke') {
        const before = rows.length;
        for (let i = rows.length - 1; i >= 0; i--) {
          if (rows[i].context === payload.context) rows.splice(i, 1);
        }
        return { ok: true, revoked: before - rows.length };
      }
      return { ok: true };
    },
  } as unknown as IpcClient;
  return { client, sent };
}

function fillForm(context: string, who: string, wording: string) {
  fireEvent.change(screen.getByLabelText(t('consent_context_label')), {
    target: { value: context },
  });
  fireEvent.change(screen.getByLabelText(t('consent_who_label')), { target: { value: who } });
  fireEvent.change(screen.getByLabelText(t('consent_wording_label')), {
    target: { value: wording },
  });
}

describe('parseConsents', () => {
  it('keeps complete rows and drops malformed ones', () => {
    const parsed = parseConsents({
      consents: [MATH, { id: 'x', context: '', wording: 'w' }, 'nonsense'],
    });
    expect(parsed).toEqual([
      {
        id: 'c1',
        context: 'lesson:math',
        who: 'Klasse 10b',
        wording: MATH.wording,
        grantedAt: MATH.granted_at,
        recordedBy: 'dashboard (dashboard)',
      },
    ]);
    expect(parseConsents(null)).toEqual([]);
  });
});

describe('validateConsent', () => {
  it('trims, requires every field and enforces the core limits', () => {
    expect(validateConsent({ context: ' a ', who: ' b ', wording: ' c ' }).grant).toEqual({
      context: 'a',
      who: 'b',
      wording: 'c',
    });
    expect(validateConsent({ context: 'a', who: '   ', wording: 'c' }).grant).toBeNull();
    const long = validateConsent({ context: 'x'.repeat(201), who: 'b', wording: 'y'.repeat(2001) });
    expect(long.grant).toBeNull();
    expect(long.tooLong).toEqual(['context', 'wording']);
    expect(
      validateConsent({ context: 'x'.repeat(200), who: 'b', wording: 'c' }).grant,
    ).not.toBeNull();
  });
});

describe('ConsentTile', () => {
  it('lists each consent with context, who, wording, time and how it was recorded', async () => {
    const { client } = fakeClient([MATH]);
    render(<ConsentTile t={t} lang="de" client={client} pinConfigured={false} />);
    await waitFor(() => expect(screen.getByText('lesson:math')).toBeTruthy());
    expect(screen.getByText('Klasse 10b')).toBeTruthy();
    expect(screen.getByText(MATH.wording)).toBeTruthy();
    expect(screen.getByText('dashboard (dashboard)')).toBeTruthy();
    expect(screen.getByTitle(MATH.granted_at).textContent).toMatch(/01\.10\.2026/);
  });

  it('sends the trimmed grant and shows the stored consent afterwards', async () => {
    const { client, sent } = fakeClient([]);
    render(<ConsentTile t={t} lang="de" client={client} pinConfigured={false} />);
    await waitFor(() => expect(screen.getByText(t('consent_empty'))).toBeTruthy());

    fillForm('  lesson:art  ', ' Klasse 7a ', '  Ja, aufnehmen ist in Ordnung.  ');
    fireEvent.click(screen.getByRole('button', { name: t('consent_grant') }));

    await waitFor(() => expect(screen.getByText('lesson:art')).toBeTruthy());
    expect(sent).toContainEqual([
      'consent.grant',
      { context: 'lesson:art', who: 'Klasse 7a', wording: 'Ja, aufnehmen ist in Ordnung.' },
    ]);
    // Reloaded from the core after the grant, not just appended locally.
    expect(sent.filter(([name]) => name === 'consent.list')).toHaveLength(2);
    expect(screen.getByText(t('consent_granted'))).toBeTruthy();
    expect((screen.getByLabelText(t('consent_context_label')) as HTMLInputElement).value).toBe('');
  });

  it('does not send while a field is empty or blank', async () => {
    const { client, sent } = fakeClient([]);
    render(<ConsentTile t={t} lang="de" client={client} pinConfigured={false} />);
    await waitFor(() => expect(screen.getByText(t('consent_empty'))).toBeTruthy());

    const submit = screen.getByRole('button', { name: t('consent_grant') }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fillForm('lesson:art', '   ', 'Ja.');
    expect(submit.disabled).toBe(true);
    fireEvent.submit(submit.closest('form') as HTMLFormElement);
    expect(sent.some(([name]) => name === 'consent.grant')).toBe(false);
    expect(screen.getByText(t('consent_required'))).toBeTruthy();
  });

  it('says which field is too long', async () => {
    const { client } = fakeClient([]);
    render(<ConsentTile t={t} lang="de" client={client} pinConfigured={false} />);
    await waitFor(() => expect(screen.getByText(t('consent_empty'))).toBeTruthy());

    fillForm('x'.repeat(201), 'Klasse 7a', 'Ja.');
    expect(screen.getByText(fill(t('consent_too_long'), 200))).toBeTruthy();
    expect(screen.getByLabelText(t('consent_context_label')).getAttribute('aria-invalid')).toBe(
      'true',
    );
  });

  it('withdraws a consent by its context', async () => {
    const { client, sent } = fakeClient([MATH]);
    render(<ConsentTile t={t} lang="de" client={client} pinConfigured={false} />);
    await waitFor(() => expect(screen.getByText('lesson:math')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: `${t('consent_withdraw')}: lesson:math` }));
    await waitFor(() => expect(screen.getByText(t('consent_empty'))).toBeTruthy());
    expect(sent).toContainEqual(['consent.revoke', { context: 'lesson:math' }]);
  });

  it('sends the PIN once when one is configured and shows the PIN message on refusal', async () => {
    const failGrant = new IpcError('permission.denied', 'consent.grant denied: wrong PIN');
    const { client, sent } = fakeClient([], { failGrant });
    render(<ConsentTile t={t} lang="de" client={client} pinConfigured />);
    await waitFor(() => expect(screen.getByText(t('consent_empty'))).toBeTruthy());

    fillForm('lesson:art', 'Klasse 7a', 'Ja.');
    const pin = screen.getByLabelText(t('consent_pin_label')) as HTMLInputElement;
    fireEvent.change(pin, { target: { value: '1234' } });
    fireEvent.click(screen.getByRole('button', { name: t('consent_grant') }));

    await waitFor(() => expect(screen.getByText(t('pin_wrong'))).toBeTruthy());
    expect(sent).toContainEqual([
      'consent.grant',
      { context: 'lesson:art', who: 'Klasse 7a', wording: 'Ja.', pin: '1234' },
    ]);
    expect(pin.value).toBe('');
    expect(screen.queryByText(/permission\.denied/)).toBeNull();
  });
});
