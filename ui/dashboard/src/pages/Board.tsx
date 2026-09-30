/**
 * The Board: what Nox drew, newest first.
 *
 * Five renderers and no more, because the core only lets Nox choose between five shapes and fill
 * them with values. Nothing on this page interprets markup — a bar is a div with a width, a table is
 * a table, and text is text. That is what makes it safe to put a language model's output here.
 *
 * The charts are plain CSS and SVG on purpose. A charting library would be a dependency, a bundle
 * and a theme to fight for five shapes that are a div and a polyline.
 */

import { useCallback, useEffect, useState } from 'react';

import { type Lang, type T } from '../i18n';
import { useRefreshOnConnect } from '../hooks';
import { type IpcClient, api } from '../ipc';
import {
  type BarsBody,
  type DrawnView,
  type FactsBody,
  type LinesBody,
  type TableBody,
  type TextBody,
  parseViews,
  scaleOf,
} from '../model';
import { Hero } from '../ui';

export interface BoardPageProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  /** Bumped by `view.shown`; a change means the board has something new. */
  revision: number;
}

function when(iso: string, lang: Lang): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return '';
  return at.toLocaleTimeString(lang === 'de' ? 'de-DE' : 'en-GB', {
    hour: '2-digit',
    minute: '2-digit',
  });
}

function Table({ body }: { body: TableBody }) {
  return (
    <table className="view-table">
      <thead>
        <tr>
          {body.columns.map((column, index) => (
            <th key={`${column}-${index}`}>{column}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {body.rows.map((row, rowIndex) => (
          <tr key={rowIndex}>
            {row.map((cell, cellIndex) => (
              <td key={cellIndex}>{cell}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Bars({ body }: { body: BarsBody }) {
  const scale = scaleOf(body.bars.map((bar) => bar.value)) || 1;
  return (
    <ul className="view-bars">
      {body.bars.map((bar, index) => (
        <li key={`${bar.label}-${index}`}>
          <span className="view-bars__label">{bar.label}</span>
          <span className="view-bars__track">
            <span
              className="view-bars__fill"
              style={{ width: `${Math.max(1, (Math.abs(bar.value) / scale) * 100)}%` }}
            />
          </span>
          <span className="view-bars__value">
            {bar.value}
            {body.unit ? ` ${body.unit}` : ''}
          </span>
        </li>
      ))}
    </ul>
  );
}

function Lines({ body }: { body: LinesBody }) {
  const all = body.series.flatMap((series) => series.values);
  const top = scaleOf(all) || 1;
  const width = 100;
  const height = 42;
  return (
    <div className="view-lines">
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-hidden>
        {body.series.map((series, index) => (
          <polyline
            key={series.name}
            className={`view-lines__line view-lines__line--${index % 4}`}
            points={series.values
              .map((value, position) => {
                const x = (position / Math.max(1, series.values.length - 1)) * width;
                const y = height - (Math.abs(value) / top) * height;
                return `${x.toFixed(2)},${y.toFixed(2)}`;
              })
              .join(' ')}
          />
        ))}
      </svg>
      <ul className="view-lines__legend">
        {body.series.map((series, index) => (
          <li key={series.name}>
            <span className={`view-lines__swatch view-lines__swatch--${index % 4}`} />
            {series.name}
          </li>
        ))}
      </ul>
      <p className="view-lines__axis">
        {body.labels[0]} … {body.labels[body.labels.length - 1]}
        {body.unit ? ` (${body.unit})` : ''}
      </p>
    </div>
  );
}

function Facts({ body }: { body: FactsBody }) {
  return (
    <dl className="view-facts">
      {body.facts.map((fact, index) => (
        <div key={`${fact.label}-${index}`}>
          <dt>{fact.label}</dt>
          <dd>{fact.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Text({ body }: { body: TextBody }) {
  return (
    <div className="view-text">
      {body.lines.map((line, index) => (
        <p key={index}>{line}</p>
      ))}
    </div>
  );
}

function Body({ view }: { view: DrawnView }) {
  switch (view.body.kind) {
    case 'table':
      return <Table body={view.body} />;
    case 'bars':
      return <Bars body={view.body} />;
    case 'lines':
      return <Lines body={view.body} />;
    case 'facts':
      return <Facts body={view.body} />;
    case 'text':
      return <Text body={view.body} />;
  }
}

export function BoardPage({ t, lang, client, revision }: BoardPageProps) {
  const [views, setViews] = useState<DrawnView[]>([]);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (c: IpcClient, cancelled: () => boolean) => {
    const payload = await api.viewsList(c);
    if (!cancelled()) setViews(parseViews(payload));
  }, []);

  useRefreshOnConnect(client, load);

  // A new view arrived while this page was open. Re-read the board rather than guessing its
  // contents from the event, which deliberately carries only the title and the kind.
  useEffect(() => {
    if (!client || revision === 0) return;
    let cancelled = false;
    void (async () => {
      const payload = await api.viewsList(client);
      if (!cancelled) setViews(parseViews(payload));
    })();
    return () => {
      cancelled = true;
    };
  }, [client, revision]);

  const clear = async () => {
    if (!client || busy) return;
    setBusy(true);
    try {
      await api.viewsClear(client);
      setViews([]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="page">
      <Hero title={t('board_title')} sub={t('board_subtitle')} />

      {views.length === 0 ? (
        <p className="empty">{t('board_empty')}</p>
      ) : (
        <>
          <div className="board-actions">
            <button type="button" className="btn" onClick={() => void clear()} disabled={busy}>
              {t('board_clear')}
            </button>
          </div>
          <div className="board">
            {views.map((view) => (
              <article key={view.id} className="tile view">
                <header className="view-head">
                  <h3>{view.body.title}</h3>
                  <span className="view-when">{when(view.created_at, lang)}</span>
                </header>
                {view.body.note && <p className="view-note">{view.body.note}</p>}
                <Body view={view} />
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
