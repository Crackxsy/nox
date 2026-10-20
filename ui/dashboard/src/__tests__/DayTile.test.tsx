/**
 * "Dein Tag": the three honest states, a reminder that can be withdrawn, and one added by hand.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { translator } from '../i18n';
import type { IpcClient } from '../ipc';
import { nextFullHour, parseDayToday } from '../model';
import { DayTile } from '../pages/home/DayTile';

const t = translator('de');

const TODAY = {
  date: '2026-10-15',
  configured: true,
  events: [
    {
      start: '2026-10-15T11:00',
      end: '2026-10-15T11:30',
      title: 'Zahnarzt',
      location: 'Springfield',
    },
    {
      start: '2026-10-15T00:00',
      end: '2026-10-16T00:00',
      title: 'Brückentag',
      all_day: true,
    },
  ],
  reminders: [
    {
      id: 'r1',
      text: 'Wasser trinken',
      due_at: '2026-10-15T09:00',
      fired: true,
    },
    { id: 'r2', text: 'Müll raus', due_at: '2026-10-15T18:00', fired: false },
  ],
  later: [{ id: 'r3', text: 'Oma anrufen', due_at: '2026-10-17T15:00', fired: false }],
  calendars: [
    {
      id: 'google',
      label: 'Google Kalender',
      configured: true,
      error: '',
      count: 2,
    },
  ],
};

function fakeClient(today: unknown) {
  const sent: [string, unknown][] = [];
  let current = today;
  const client = {
    request: async (name: string, payload: Record<string, unknown> = {}) => {
      sent.push([name, payload]);
      if (name === 'day.today') return current;
      if (name === 'day.reminder.cancel') {
        const day = current as typeof TODAY;
        current = {
          ...day,
          reminders: day.reminders.filter((r) => r.id !== payload.id),
        };
        return { ok: true };
      }
      if (name === 'day.reminder.add') {
        const day = current as typeof TODAY;
        current = {
          ...day,
          later: [
            ...day.later,
            {
              id: 'r9',
              text: String(payload.text),
              due_at: String(payload.due_at),
              fired: false,
            },
          ],
        };
        return { ok: true, id: 'r9' };
      }
      return { ok: true };
    },
  } as unknown as IpcClient;
  return { client, sent };
}

describe('parseDayToday', () => {
  it('keeps well-formed entries and drops the rest', () => {
    const day = parseDayToday({
      ...TODAY,
      events: [...TODAY.events, { title: 'no start' }, 7],
    });
    expect(day.events.map((e) => e.title)).toEqual(['Zahnarzt', 'Brückentag']);
    expect(day.events[1].allDay).toBe(true);
    expect(day.reminders.map((r) => r.fired)).toEqual([true, false]);
    expect(parseDayToday(null).configured).toBe(false);
  });

  it('suggests the next full hour for a new reminder', () => {
    expect(nextFullHour(new Date(2026, 9, 15, 21, 17))).toBe('2026-10-15T22:00');
    expect(nextFullHour(new Date(2026, 9, 15, 23, 5))).toBe('2026-10-16T00:00');
  });
});

describe('DayTile', () => {
  it('shows appointments, spoken and open reminders, and withdraws one', async () => {
    const { client, sent } = fakeClient(TODAY);
    render(<DayTile t={t} lang="de" client={client} onOpenSettings={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Zahnarzt')).toBeTruthy());
    expect(screen.getByText('Springfield')).toBeTruthy();
    expect(screen.getByText(t('day_all_day'))).toBeTruthy();
    expect(screen.getByText(t('day_fired'))).toBeTruthy();
    expect(
      screen.queryByRole('button', {
        name: `${t('day_cancel')}: Wasser trinken`,
      }),
    ).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: `${t('day_cancel')}: Müll raus` }));
    await waitFor(() => expect(screen.queryByText('Müll raus')).toBeNull());
    expect(sent).toContainEqual(['day.reminder.cancel', { id: 'r2' }]);
    expect(screen.getByText('Oma anrufen')).toBeTruthy();
  });

  it('points to the settings while no calendar is connected', async () => {
    const onOpenSettings = vi.fn();
    const { client } = fakeClient({
      ...TODAY,
      configured: false,
      events: [],
      calendars: [],
    });
    render(<DayTile t={t} lang="de" client={client} onOpenSettings={onOpenSettings} />);
    await waitFor(() => expect(screen.getByText(t('day_no_calendar'))).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: t('day_no_calendar_link') }));
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
  });

  it('names a calendar that could not be loaded, in the core\u2019s words', async () => {
    const failing = {
      id: 'school',
      label: 'Stundenplan',
      configured: true,
      error: 'HTTP 404',
      count: 0,
    };
    const { client } = fakeClient({
      ...TODAY,
      calendars: [...TODAY.calendars, failing],
    });
    render(<DayTile t={t} lang="de" client={client} onOpenSettings={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/HTTP 404/)).toBeTruthy());
  });

  it('adds a reminder typed in by hand', async () => {
    const { client, sent } = fakeClient({ ...TODAY, reminders: [], later: [] });
    render(<DayTile t={t} lang="de" client={client} onOpenSettings={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(t('day_reminders_empty'))).toBeTruthy());
    fireEvent.change(screen.getByLabelText(t('day_add_label')), {
      target: { value: 'Oma anrufen' },
    });
    fireEvent.change(screen.getByLabelText(t('day_add_when')), {
      target: { value: '2026-10-17T15:00' },
    });
    fireEvent.click(screen.getByRole('button', { name: t('day_add') }));
    await waitFor(() => expect(screen.getByText('Oma anrufen')).toBeTruthy());
    expect(sent).toContainEqual([
      'day.reminder.add',
      { text: 'Oma anrufen', due_at: '2026-10-17T15:00' },
    ]);
  });
});
