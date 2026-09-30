import { describe, expect, it } from 'vitest';

import { parseViews, scaleOf } from '../model';

const TABLE = {
  id: 'v1',
  created_at: '2026-09-30T20:00:00+00:00',
  body: {
    kind: 'table',
    title: 'Offene Aufgaben',
    note: 'zwei davon diese Woche',
    columns: ['Titel', 'Fällig'],
    rows: [
      ['Steuer', 'Freitag'],
      ['Impfung', 'Montag'],
    ],
  },
};

describe('parseViews', () => {
  it('reads a table', () => {
    const [view] = parseViews({ views: [TABLE] });

    expect(view.id).toBe('v1');
    expect(view.body.kind).toBe('table');
    expect(view.body).toMatchObject({ columns: ['Titel', 'Fällig'] });
  });

  it('drops a table whose rows do not fit its columns', () => {
    // The core rejects this shape, so a payload that still has one is a bug — and a ragged table
    // on screen looks like data. An empty board is the honest rendering of a broken payload.
    const ragged = { ...TABLE, body: { ...TABLE.body, rows: [['nur eins']] } };

    expect(parseViews({ views: [ragged] })).toEqual([]);
  });

  it('drops a line chart whose series do not match its labels', () => {
    const view = {
      id: 'v2',
      created_at: '',
      body: {
        kind: 'lines',
        title: 'Woche',
        note: '',
        labels: ['Mo', 'Di', 'Mi'],
        series: [{ name: 'Stunden', values: [1, 2] }],
        unit: 'h',
      },
    };

    expect(parseViews({ views: [view] })).toEqual([]);
  });

  it('keeps a line chart that lines up', () => {
    const view = {
      id: 'v3',
      created_at: '',
      body: {
        kind: 'lines',
        title: 'Woche',
        note: '',
        labels: ['Mo', 'Di'],
        series: [{ name: 'Stunden', values: [1, 2.5] }],
        unit: 'h',
      },
    };

    const [parsed] = parseViews({ views: [view] });

    expect(parsed.body).toMatchObject({ kind: 'lines', unit: 'h' });
  });

  it('drops a shape it does not know', () => {
    expect(parseViews({ views: [{ id: 'x', body: { kind: 'pie', title: 'Nope' } }] })).toEqual([]);
  });

  it('drops an empty shape rather than drawing an empty chart', () => {
    const empty = { id: 'x', body: { kind: 'bars', title: 'Leer', note: '', bars: [], unit: '' } };

    expect(parseViews({ views: [empty] })).toEqual([]);
  });

  it('survives a payload that is not a board at all', () => {
    expect(parseViews({})).toEqual([]);
    expect(parseViews({ views: 'nope' })).toEqual([]);
    expect(parseViews({ views: [null, 42, 'x'] })).toEqual([]);
  });

  it('turns a non-number bar value into zero instead of NaN on screen', () => {
    const view = {
      id: 'x',
      body: {
        kind: 'bars',
        title: 'Platz',
        note: '',
        bars: [
          { label: 'C:', value: 'viel' },
          { label: 'D:', value: 12 },
        ],
        unit: 'GB',
      },
    };

    const [parsed] = parseViews({ views: [view] });

    expect(parsed.body).toMatchObject({ bars: [{ label: 'C:', value: 0 }, { label: 'D:', value: 12 }] });
  });
});

describe('scaleOf', () => {
  it('is the largest absolute value', () => {
    expect(scaleOf([1, -9, 4])).toBe(9);
  });

  it('is zero for nothing, so a caller has to guard the division', () => {
    expect(scaleOf([])).toBe(0);
  });
});
