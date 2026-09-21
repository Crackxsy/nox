/**
 * Value formatting for screens: timestamps, durations and the em-dash placeholder.
 *
 * The core speaks ISO-8601 with microseconds (`2026-09-16T22:18:07.488120Z`). That is the right
 * thing to log and the wrong thing to read, so every table and fact list runs its timestamps
 * through `formatTimestamp` and keeps the exact value in a `title` (`timestampTitle`) for anyone
 * who needs it. An unparsable value is passed through verbatim rather than replaced by a guess.
 */

export type FormatLang = 'de' | 'en';

const LOCALE: Record<FormatLang, string> = { de: 'de-DE', en: 'en-GB' };

/** The placeholder for "the core sent nothing here". One character, one meaning, one place. */
export const DASH = '–';

export function dash(value: string | null | undefined): string {
  return value === null || value === undefined || value === '' ? DASH : value;
}

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

/**
 * "heute, 22:18 Uhr" / "16.09.2026, 22:18 Uhr" — today's times drop the date, everything else
 * keeps it. `now` is injected so tests do not depend on the wall clock.
 */
export function formatTimestamp(
  iso: string | null | undefined,
  lang: FormatLang,
  now: Date = new Date(),
): string {
  if (!iso) return DASH;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  const locale = LOCALE[lang];
  const time = at.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' });
  if (sameDay(at, now)) {
    return lang === 'de' ? `heute, ${time} Uhr` : `today, ${time}`;
  }
  const date = at.toLocaleDateString(locale, { day: '2-digit', month: '2-digit', year: 'numeric' });
  return lang === 'de' ? `${date}, ${time} Uhr` : `${date}, ${time}`;
}

/** The exact value, for the `title` attribute next to a formatted one. */
export function timestampTitle(iso: string | null | undefined): string | undefined {
  return iso ? iso : undefined;
}

/** "12,4 s" / "1:04 min" — clip lengths and latencies, never a bare float. */
export function formatDuration(seconds: number | null | undefined, lang: FormatLang): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return DASH;
  if (seconds < 60) {
    const value = seconds.toFixed(1);
    return lang === 'de' ? `${value.replace('.', ',')} s` : `${value} s`;
  }
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  return `${minutes}:${String(rest).padStart(2, '0')} min`;
}

/** "2,5 s" for a millisecond latency; below a second it stays in milliseconds. */
export function formatLatency(ms: number | null | undefined, lang: FormatLang): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return DASH;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const value = (ms / 1000).toFixed(1);
  return lang === 'de' ? `${value.replace('.', ',')} s` : `${value} s`;
}
