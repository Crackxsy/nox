/**
 * Views as the dashboard sees them.
 *
 * The core validates every view before it reaches the board, so these types mirror
 * `nox.views.model` rather than inventing a view model. Parsing is still defensive in one
 * direction: a payload that is missing or the wrong shape yields an empty board, never a
 * half-built chart that looks like data and is not.
 *
 * Nothing here is markup. A view is values and a shape, and the page decides how it looks - which
 * is the whole reason the model is not allowed to send HTML.
 */

export type ViewKind = 'table' | 'bars' | 'lines' | 'facts' | 'text';

export interface Fact {
  label: string;
  value: string;
}

export interface Bar {
  label: string;
  value: number;
}

export interface Series {
  name: string;
  values: number[];
}

interface Shape {
  title: string;
  note: string;
}

export interface TableBody extends Shape {
  kind: 'table';
  columns: string[];
  rows: string[][];
}

export interface BarsBody extends Shape {
  kind: 'bars';
  bars: Bar[];
  unit: string;
}

export interface LinesBody extends Shape {
  kind: 'lines';
  labels: string[];
  series: Series[];
  unit: string;
}

export interface FactsBody extends Shape {
  kind: 'facts';
  facts: Fact[];
}

export interface TextBody extends Shape {
  kind: 'text';
  lines: string[];
}

export type ViewBody = TableBody | BarsBody | LinesBody | FactsBody | TextBody;

export interface DrawnView {
  id: string;
  created_at: string;
  body: ViewBody;
}

const KINDS: ViewKind[] = ['table', 'bars', 'lines', 'facts', 'text'];

function str(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function num(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(str) : [];
}

function numbers(value: unknown): number[] {
  return Array.isArray(value) ? value.map(num) : [];
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function body(raw: Record<string, unknown>): ViewBody | null {
  const kind = str(raw.kind) as ViewKind;
  if (!KINDS.includes(kind)) return null;
  const shape = { title: str(raw.title), note: str(raw.note) };
  switch (kind) {
    case 'table': {
      const columns = strings(raw.columns);
      const rows = (Array.isArray(raw.rows) ? raw.rows : []).map(strings);
      // A row that does not fit the columns cannot be drawn; the core rejects it, and a payload
      // that still has one is a bug worth showing as an empty table rather than a ragged one.
      return columns.length && rows.every((row) => row.length === columns.length)
        ? { ...shape, kind, columns, rows }
        : null;
    }
    case 'bars': {
      const bars = (Array.isArray(raw.bars) ? raw.bars : []).map((entry) => {
        const one = record(entry);
        return { label: str(one.label), value: num(one.value) };
      });
      return bars.length ? { ...shape, kind, bars, unit: str(raw.unit) } : null;
    }
    case 'lines': {
      const labels = strings(raw.labels);
      const series = (Array.isArray(raw.series) ? raw.series : []).map((entry) => {
        const one = record(entry);
        return { name: str(one.name), values: numbers(one.values) };
      });
      const usable =
        labels.length > 0 &&
        series.length > 0 &&
        series.every((line) => line.values.length === labels.length);
      return usable ? { ...shape, kind, labels, series, unit: str(raw.unit) } : null;
    }
    case 'facts': {
      const facts = (Array.isArray(raw.facts) ? raw.facts : []).map((entry) => {
        const one = record(entry);
        return { label: str(one.label), value: str(one.value) };
      });
      return facts.length ? { ...shape, kind, facts } : null;
    }
    case 'text': {
      const lines = strings(raw.lines);
      return lines.length ? { ...shape, kind, lines } : null;
    }
  }
}

/** Every drawable view in a `views.list` payload, newest first. Unusable entries are dropped. */
export function parseViews(payload: Record<string, unknown>): DrawnView[] {
  const raw = Array.isArray(payload.views) ? payload.views : [];
  const out: DrawnView[] = [];
  for (const entry of raw) {
    const one = record(entry);
    const parsed = body(record(one.body));
    if (parsed) out.push({ id: str(one.id), created_at: str(one.created_at), body: parsed });
  }
  return out;
}

/** The largest absolute value in a set of bars or series, for scaling. 0 when there is nothing. */
export function scaleOf(values: number[]): number {
  return values.reduce((most, value) => Math.max(most, Math.abs(value)), 0);
}
