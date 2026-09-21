/**
 * Audit page: the `security.audit` events received over this connection (A258: every action,
 * including denied, aborted and failed ones, is auditable). This view is explicitly only the live
 * session feed — the append-only log itself lives in the core, and the page says so instead of
 * pretending to show the complete history.
 *
 * Every column that carries an identifier shows the word on top and the core's own value beneath
 * it, so the table reads as German prose without losing a single machine-readable token.
 *
 * The row count is announced as a sentence, and only when the *filter* changes — announcing a bare
 * number on every incoming event turned a screen reader into a ticker.
 */

import { useEffect, useMemo, useRef, useState } from 'react';

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
import { Detail, Hero, StateWord, Tile, toneFor } from '../ui';

export interface AuditPageProps {
  t: T;
  lang: Lang;
  rows: AuditRow[];
}

const COLUMNS: Key[] = [
  'audit_seq',
  'audit_time',
  'audit_actor',
  'audit_tool',
  'audit_action',
  'audit_target',
  'audit_decision',
  'audit_result',
];

export function AuditPage({ t, lang, rows }: AuditPageProps) {
  const [filter, setFilter] = useState('');
  const [decisionFilter, setDecisionFilter] = useState('');
  const [resultFilter, setResultFilter] = useState('');
  const [announcement, setAnnouncement] = useState('');

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

  const count = shown.length;
  const filtered = filter !== '' || decisionFilter !== '' || resultFilter !== '';

  // Announce the count when the *filter* changes, not when a new event arrives. `countRef` keeps
  // the number out of the dependency list, so an incoming audit event does not re-announce.
  const countRef = useRef(count);
  countRef.current = count;
  useEffect(() => {
    setAnnouncement(fill(t('audit_count'), countRef.current));
  }, [filter, decisionFilter, resultFilter, t]);

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
        <Tile id="audit-filter" title={t('audit_title')} lede={t('audit_hint')}>
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
                    {auditDecisionLabel(t, d)}
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
                    {auditResultLabel(t, r)}
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
            <p className="rail-sub">{fill(t('audit_count'), count)}</p>
            <p role="status" aria-live="polite" className="sr-only">
              {announcement}
            </p>
          </div>
        </div>

        {count === 0 ? (
          <p className="muted">{t('audit_empty')}</p>
        ) : (
          <div className="table-scroll">
            <table className="specs">
              <caption className="sr-only">{t('audit_title')}</caption>
              <thead>
                <tr>
                  {COLUMNS.map((k) => (
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
                    <td className="muted nowrap" title={r.ts}>
                      {formatTimestamp(r.ts, lang)}
                    </td>
                    <td>
                      {r.actor ? (
                        <Detail label={auditActorLabel(t, r.actor)} detail={r.actor} />
                      ) : (
                        '–'
                      )}
                    </td>
                    <td>
                      {r.tool ? <Detail label={auditToolLabel(t, r.tool)} detail={r.tool} /> : '–'}
                    </td>
                    <td>
                      {r.action ? (
                        <Detail label={auditActionLabel(t, r.action)} detail={r.action} />
                      ) : (
                        '–'
                      )}
                    </td>
                    <td className="break">{r.target || '–'}</td>
                    <td>
                      {r.decision ? (
                        <StateWord
                          tone={toneFor(r.decision)}
                          label={auditDecisionLabel(t, r.decision)}
                        />
                      ) : (
                        '–'
                      )}
                    </td>
                    <td>
                      {r.result ? (
                        <StateWord tone={toneFor(r.result)} label={auditResultLabel(t, r.result)} />
                      ) : (
                        '–'
                      )}
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
