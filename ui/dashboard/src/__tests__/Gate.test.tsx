/**
 * The pairing screen a new device sees, and what it sends.
 */

import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { PairScreen } from '../Gate';
import { translator } from '../i18n';
import { isIosBrowserTab, isStandalone } from '../platform';

const t = translator('de');
const TOKEN = 'x'.repeat(43);

function fetchAnswering(status: number, body: unknown) {
  return vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status }));
}

describe('the pairing screen', () => {
  it('starts with an empty name, the device type as a placeholder and a hint what empty means', () => {
    render(<PairScreen t={t} code="ABCDEFGH" onPaired={() => undefined} defaultName="iPhone" fetchImpl={vi.fn()} />);
    const name = screen.getByLabelText(t('pair_name')) as HTMLInputElement;
    expect(name.value).toBe('');
    expect(name.placeholder).toBe('iPhone');
    expect(name.getAttribute('aria-describedby')).toBe('pair-name-hint');
    expect(screen.getByText(t('pair_name_hint'))).toBeTruthy();
  });

  it('sends what was typed and its own guess separately', async () => {
    const fetchImpl = fetchAnswering(201, { device_id: 'a', client_id: 'device:a', name: 'iPhone', token: TOKEN });
    const paired = vi.fn();
    render(<PairScreen t={t} code="ABCDEFGH" onPaired={paired} defaultName="iPhone" fetchImpl={fetchImpl} />);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('pair_button') }));
    });
    const body = JSON.parse(String(fetchImpl.mock.calls[0]![1].body));
    expect(body).toEqual({ code: 'ABCDEFGH', name: '', default_name: 'iPhone' });
    expect(paired).toHaveBeenCalledOnce();
  });

  it('after a failed code from the link, offers the code field for a new one', async () => {
    const fetchImpl = fetchAnswering(401, { error: 'used' });
    render(<PairScreen t={t} code="ABCDEFGH" onPaired={() => undefined} defaultName="iPhone" fetchImpl={fetchImpl} />);
    expect(screen.queryByLabelText(t('pair_code'))).toBeNull();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('pair_button') }));
    });
    expect(screen.getByRole('alert').textContent).toBe(t('pair_error_used'));
    const code = screen.getByLabelText(t('pair_code')) as HTMLInputElement;
    expect(code.getAttribute('aria-describedby')).toBe('pair-error');
    fireEvent.change(code, { target: { value: 'jkmn pqrs' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('pair_button') }));
    });
    expect(JSON.parse(String(fetchImpl.mock.calls[1]![1].body)).code).toBe('JKMNPQRS');
  });
});

describe('where this page runs', () => {
  const ipad = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15';

  it('knows Safari on iOS from the app on the home screen', () => {
    expect(isIosBrowserTab({ userAgent: 'iPhone', maxTouchPoints: 5, displayStandalone: false })).toBe(true);
    expect(isIosBrowserTab({ userAgent: ipad, maxTouchPoints: 5, displayStandalone: false })).toBe(true);
    expect(isIosBrowserTab({ userAgent: ipad, maxTouchPoints: 0, displayStandalone: false })).toBe(false);
    expect(isIosBrowserTab({ userAgent: 'iPhone', maxTouchPoints: 5, standalone: true, displayStandalone: false })).toBe(false);
    expect(isStandalone({ displayStandalone: true })).toBe(true);
    expect(isStandalone({ standalone: false, displayStandalone: false })).toBe(false);
  });
});
