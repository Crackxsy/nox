/** The last health changes per component, as a tech-specs table. Read-only, newest first. */

import { formatTimestamp } from '../../../../shared/format';
import { type Lang, type T, componentLabel, reasonLine, statusLabel } from '../../i18n';
import type { HealthHistoryEntry } from '../../model';
import { Detail, StateWord, toneFor } from '../../ui';

export interface HealthHistoryTableProps {
  t: T;
  lang: Lang;
  history: HealthHistoryEntry[];
}

export function HealthHistoryTable({ t, lang, history }: HealthHistoryTableProps) {
  return (
    <section aria-labelledby="h-health-history" id="settings-health">
      <div className="rail-head">
        <div>
          <h3 id="h-health-history" className="rail-title">
            {t('settings_health_title')}
          </h3>
          <p className="rail-sub">{t('settings_health_hint')}</p>
        </div>
      </div>
      {history.length === 0 ? (
        <p className="muted">{t('settings_health_empty')}</p>
      ) : (
        <div className="table-scroll">
          <table className="specs">
            <caption className="sr-only">{t('settings_health_title')}</caption>
            <thead>
              <tr>
                <th scope="col">{t('settings_col_component')}</th>
                <th scope="col">{t('settings_col_status')}</th>
                <th scope="col">{t('settings_col_time')}</th>
                <th scope="col">{t('settings_col_reason')}</th>
              </tr>
            </thead>
            <tbody>
              {history.map((h) => {
                const reason = reasonLine(t, lang, h.reason);
                return (
                  <tr key={h.id}>
                    <th scope="row">
                      {h.component ? (
                        <Detail label={componentLabel(t, h.component)} detail={h.component} />
                      ) : (
                        '–'
                      )}
                    </th>
                    <td>
                      <StateWord tone={toneFor(h.status)} label={statusLabel(t, h.status)} />
                    </td>
                    <td className="muted nowrap" title={h.ts}>
                      {formatTimestamp(h.ts, lang)}
                    </td>
                    <td className="muted break">
                      {reason.text ? <Detail label={reason.text} detail={reason.original} /> : '–'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
