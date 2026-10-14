/**
 * The user's day: the `day.today` payload - appointments from the connected calendars, today's
 * reminders (spoken ones included), the next few after today, and what each feed is up to.
 *
 * Narrowed by hand through `shared/guards` like `home.ts`: `day.*` is answered by the `day`
 * extension, not by a pydantic model in `src/nox/ipc/protocol.py`. Nothing is invented - an entry
 * the core did not send does not appear, and a feed error arrives in the core's words.
 */

import { bool, isRecord, num, str } from '../../../shared/guards';

export interface DayEvent {
  /** Local ISO-8601 date-times, minute precision, as the core formats them. */
  start: string;
  end: string;
  title: string;
  location: string;
  allDay: boolean;
  source: string;
}

export interface DayReminder {
  id: string;
  text: string;
  dueAt: string;
  /** Already spoken today. */
  fired: boolean;
  source: string;
}

export interface DayFeed {
  id: string;
  label: string;
  configured: boolean;
  /** Empty when the last fetch worked; never contains the feed URL. */
  error: string;
  count: number;
}

export interface DayToday {
  date: string;
  /** Whether at least one calendar has a URL stored. */
  configured: boolean;
  events: DayEvent[];
  reminders: DayReminder[];
  later: DayReminder[];
  calendars: DayFeed[];
}

export const EMPTY_DAY: DayToday = {
  date: '',
  configured: false,
  events: [],
  reminders: [],
  later: [],
  calendars: [],
};

function parseReminder(entry: unknown): DayReminder | null {
  if (!isRecord(entry)) return null;
  const id = str(entry.id);
  const text = str(entry.text);
  if (!id || !text) return null;
  return {
    id,
    text,
    dueAt: str(entry.due_at),
    fired: bool(entry.fired),
    source: str(entry.source),
  };
}

function parseEvent(entry: unknown): DayEvent | null {
  if (!isRecord(entry)) return null;
  const start = str(entry.start);
  if (!start) return null;
  return {
    start,
    end: str(entry.end),
    title: str(entry.title),
    location: str(entry.location),
    allDay: bool(entry.all_day),
    source: str(entry.source),
  };
}

function parseFeed(entry: unknown): DayFeed | null {
  if (!isRecord(entry)) return null;
  const id = str(entry.id);
  if (!id) return null;
  return {
    id,
    label: str(entry.label, id),
    configured: bool(entry.configured),
    error: str(entry.error),
    count: num(entry.count, 0),
  };
}

function list<T>(raw: unknown, parse: (entry: unknown) => T | null): T[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(parse).filter((item): item is T => item !== null);
}

/** `day.today` reply -> the page's model; a malformed reply is an empty day, not a crash. */
export function parseDayToday(payload: unknown): DayToday {
  if (!isRecord(payload)) return EMPTY_DAY;
  return {
    date: str(payload.date),
    configured: bool(payload.configured),
    events: list(payload.events, parseEvent),
    reminders: list(payload.reminders, parseReminder),
    later: list(payload.later, parseReminder),
    calendars: list(payload.calendars, parseFeed),
  };
}

/** "09:00" from a local ISO date-time; the string itself when it is not one. */
export function clockTime(iso: string, lang: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleTimeString(lang, { hour: '2-digit', minute: '2-digit' });
}

/** "Do., 15.10." for a reminder that is not today. */
export function shortDate(iso: string, lang: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString(lang, {
    weekday: 'short',
    day: '2-digit',
    month: '2-digit',
  });
}

/** The value a `datetime-local` input wants for "the next full hour from now". */
export function nextFullHour(now: Date = new Date()): string {
  const next = new Date(now);
  next.setMinutes(0, 0, 0);
  next.setHours(next.getHours() + 1);
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${next.getFullYear()}-${pad(next.getMonth() + 1)}-${pad(next.getDate())}` +
    `T${pad(next.getHours())}:${pad(next.getMinutes())}`
  );
}
