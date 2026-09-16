/**
 * Audit page: the `security.audit` events received over this connection (A258: every action,
 * including denied, aborted and failed ones, is auditable). This view is explicitly only the live
 * session feed — the append-only log itself lives in the core, and the page says so instead of
 * pretending to show the complete history.
 */

import { useMemo, useState } from 'react';

import type { T } from '../i18n';
import type { AuditRow } from '../model';
import { Hero, StateWord, Tile } from '../ui';

export interface AuditPageProps {
  t: T;
  rows: AuditRow[];
}

export function AuditPage({ t, rows }: AuditPageProps) {
  const [filter, setFilter] = useState('');
  const [decisionFilter, setDecisionFilter] = useState('');
  const [resultFilter, setResultFilter] = useState('');

  const decisions = useMemo(
    () => Array.from(new Set(rows.map((r) => r.decision).filter(Boolean))).sort(),
    [rows],
  );
  const results = useMemo(
    () => Array.from(new Set(rows.map((r) => r.result).filter(Boolean))).sort(),
    [rows],
  );

  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return rows.filter((r) => {
      if (decisionFilter && r.decision !== decisionFilter) return false;
      if (resultFilter && r.result !== resultFilter) return false;
      if (!q) return true;
      return [r.actor, r.tool, r.action, r.target, r.decision, r.result].some((v) =>
        v.toLowerCase().includes(q),
      );
    });
  }, [rows, filter, decisionFilter, resultFilter]);

  const filtered = filter !== '' || decisionFilter !== '' || resultFilter !== '';

  const reset = () => {
    setFilter('');
    setDecisionFilter('');
    setResultFilter('');
  };

  return (
    <div className="page wrap">
      <Hero
        title={t('hero_audit')}
        sub={t('hero_audit_sub')}
        links={
          <button type="button" className="link" onClick={reset} disabled={!filtered}>
            {t('audit_reset')}
          </button>
        }
      />

      <div className="tiles tiles--single">
        <Tile id="audit-filter" eyebrow={t('audit_title')} title={t('audit_filter')} lede={t('audit_hint')}>
          <div className="row-controls">
            <div className="field field--grow">
              <label htmlFor="audit-filter-text" className="label">
                {t('audit_filter')}
              </label>
              <input
                id="audit-filter-text"
                type="search"
                className="input"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') setFilter('');
                }}
              />
            </div>
            <div className="field">
              <label htmlFor="audit-filter-decision" className="label">
                {t('audit_filter_decision')}
              </label>
              <select
                id="audit-filter-decision"
                className="select"
                value={decisionFilter}
                onChange={(e) => setDecisionFilter(e.target.value)}
              >
                <option value="">{t('audit_filter_all')}</option>
                {decisions.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="audit-filter-result" className="label">
                {t('audit_filter_result')}
              </label>
              <select
                id="audit-filter-result"
                className="select"
                value={resultFilter}
                onChange={(e) => setResultFilter(e.target.value)}
              >
                <option value="">{t('audit_filter_all')}</option>
                {results.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </Tile>
      </div>

      <section aria-labelledby="h-audit-rows">
        <div className="rail-head">
          <div>
            <h3 id="h-audit-rows" className="rail-title">
              {t('audit_title')}
            </h3>
            <p role="status" aria-live="polite" className="sr-only">
              {shown.length}
            </p>
          </div>
        </div>

        {shown.length === 0 ? (
          <p className="muted">{t('audit_empty')}</p>
        ) : (
          <div className="table-scroll">
            <table className="specs">
              <caption className="sr-only">{t('audit_title')}</caption>
              <thead>
                <tr>
                  {(
                    [
                      'audit_seq',
                      'audit_time',
                      'audit_actor',
                      'audit_tool',
                      'audit_action',
                      'audit_target',
                      'audit_decision',
                      'audit_result',
                    ] as const
                  ).map((k) => (
                    <th key={k} scope="col">
                      {t(k)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={r.seq}>
                    <th scope="row" className="num">
                      {r.seq}
                    </th>
                    <td className="muted nowrap">{r.ts || '–'}</td>
                    <td>{r.actor || '–'}</td>
                    <td>{r.tool || '–'}</td>
                    <td>{r.action || '–'}</td>
                    <td className="break">{r.target || '–'}</td>
                    <td>{r.decision || '–'}</td>
                    <td>
                      {r.result ? <StateWord status={r.result} label={r.result} /> : '–'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
