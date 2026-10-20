/**
 * Nox › Verlauf ("Was Nox getan hat", formerly the audit page): the `security.audit` events this
 * connection received, newest first, in plain words - what, where, and how it ended - with the
 * identifiers behind "Technische Angaben" on each row.
 *
 * It is the live session feed and says so: the append-only log itself lives in the core. The
 * filter is four words a person understands (Alles, Erlaubt, Abgelehnt, Fehler) instead of the
 * core's decision and result enums.
 */

import { useState } from 'react';

import { formatTimestamp } from '../../../shared/format';
import {
  type Key,
  type Lang,
  type T,
  auditActionLabel,
  auditActorLabel,
  auditDecisionLabel,
  auditResultLabel,
  auditToolLabel,
  fill,
} from '../i18n';
import type { AuditRow } from '../model';

export type HistoryFilter = 'all' | 'allowed' | 'denied' | 'failed';

const FILTERS: { id: HistoryFilter; label: Key }[] = [
  { id: 'all', label: 'ux_hist_all' },
  { id: 'allowed', label: 'ux_hist_allowed' },
  { id: 'denied', label: 'ux_hist_denied' },
  { id: 'failed', label: 'ux_hist_failed' },
];

/** Which of the four words a row falls under. */
export function outcomeOf(row: AuditRow): Exclude<HistoryFilter, 'all'> | 'asked' {
  if (row.result === 'failed' || row.result === 'aborted') return 'failed';
  if (row.decision === 'deny' || row.result === 'denied') return 'denied';
  if (row.decision === 'confirm' && row.result !== 'ok') return 'asked';
  return 'allowed';
}

const OUTCOME: Record<ReturnType<typeof outcomeOf>, { key: Key; tone: string }> = {
  allowed: { key: 'ux_hist_outcome_allowed', tone: 'badge--ok' },
  denied: { key: 'ux_hist_outcome_denied', tone: 'badge--danger' },
  failed: { key: 'ux_hist_outcome_failed', tone: 'badge--danger' },
  asked: { key: 'ux_hist_outcome_asked', tone: 'badge--attn' },
};

export interface HistoryPageProps {
  t: T;
  lang: Lang;
  rows: AuditRow[];
}

export function HistoryPage({ t, lang, rows }: HistoryPageProps) {
  const [filter, setFilter] = useState<HistoryFilter>('all');
  const shown = filter === 'all' ? rows : rows.filter((r) => outcomeOf(r) === filter);

  // The count is announced when the filter (or the language) changes, not on every new event: the
  // announcement remembers what it was made for and is redone, while rendering, only then.
  const [announced, setAnnounced] = useState<{ filter: HistoryFilter; t: T; text: string } | null>(null);
  if (announced === null || announced.filter !== filter || announced.t !== t) {
    setAnnounced({ filter, t, text: fill(t('audit_count'), shown.length) });
  }

  return (
    <div className="history">
      <p className="lede">{t('ux_hist_lede')}</p>
      <div role="group" aria-label={t('ux_hist_filter')} className="seg">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            aria-pressed={filter === f.id}
            onClick={() => setFilter(f.id)}
          >
            {t(f.label)}
          </button>
        ))}
      </div>
      <p role="status" className="hint">
        {announced?.text}
      </p>
      {rows.length === 0 ? (
        <p className="muted">{t('ux_hist_empty')}</p>
      ) : shown.length === 0 ? (
        <p className="muted">{t('ux_hist_none')}</p>
      ) : (
        <ul className="list list--rows" aria-label={t('ux_hist_title')}>
          {shown.map((row) => {
            const outcome = OUTCOME[outcomeOf(row)];
            return (
              <li key={row.seq} className="row row--top">
                <span className="grow">
                  <span className="row-t">
                    {auditToolLabel(t, row.tool)} – {auditActionLabel(t, row.action)}
                  </span>
                  {row.target && <span className="row-d break">{row.target}</span>}
                  <span className="row-d">
                    {auditActorLabel(t, row.actor)} · <span title={row.ts}>{formatTimestamp(row.ts, lang)}</span>
                  </span>
                  <details className="tech">
                    <summary>{t('perm_technical')}</summary>
                    <dl className="tech-list">
                      <dt>{t('audit_seq')}</dt>
                      <dd className="mono">{row.seq}</dd>
                      <dt>{t('audit_actor')}</dt>
                      <dd className="mono">{row.actor}</dd>
                      <dt>{t('audit_tool')}</dt>
                      <dd className="mono">{row.tool}</dd>
                      <dt>{t('audit_action')}</dt>
                      <dd className="mono">{row.action}</dd>
                      <dt>{t('audit_decision')}</dt>
                      <dd>{row.decision ? auditDecisionLabel(t, row.decision) : '–'}</dd>
                      <dt>{t('audit_result')}</dt>
                      <dd>{row.result ? auditResultLabel(t, row.result) : '–'}</dd>
                      {row.taskId && (
                        <>
                          <dt>{t('ux_hist_task')}</dt>
                          <dd className="mono">{row.taskId}</dd>
                        </>
                      )}
                    </dl>
                  </details>
                </span>
                <span className={`badge ${outcome.tone}`}>{t(outcome.key)}</span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
