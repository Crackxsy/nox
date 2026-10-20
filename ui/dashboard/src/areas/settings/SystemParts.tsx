/**
 * The facts the old Status page showed, each on the Settings page it belongs to now (Spec UX-Neubau
 * §6): the health report on System, the AI providers on KI, the voice pipeline's live facts on
 * Stimme › Mehr Optionen. Words first, the core's identifiers underneath (`Detail`).
 *
 * The health report stays a real <table> (component row header, status cell, reason cell): it is a
 * data grid, and the e2e smoke test reads it as one.
 */

import { formatTimestamp } from '../../../../shared/format';
import { type Lang, type T, componentLabel, reasonLine, roleList, routingLabel, statusLabel } from '../../i18n';
import type { DashboardState } from '../../model';
import { Detail, StateWord, toneFor } from '../../ui';

export function HealthTable({ t, lang, state }: { t: T; lang: Lang; state: DashboardState }) {
  const health = state.health;
  return (
    <section className="sec" aria-labelledby="h-health">
      <div className="sec-h">
        <h2 id="h-health" className="sec-title">
          {t('health_title')}
        </h2>
      </div>
      {health?.generatedAt && (
        <p className="hint">
          {t('health_generated')}: <span title={health.generatedAt}>{formatTimestamp(health.generatedAt, lang)}</span>
        </p>
      )}
      {health && health.components.length > 0 ? (
        <div className="table-scroll">
          <table className="specs">
            <caption className="sr-only">{t('health_title')}</caption>
            <thead>
              <tr>
                <th scope="col">{t('health_component')}</th>
                <th scope="col">{t('health_status')}</th>
                <th scope="col">{t('health_reason')}</th>
              </tr>
            </thead>
            <tbody>
              {health.components.map((c) => {
                const reason = reasonLine(t, lang, c.reason);
                return (
                  <tr key={c.component}>
                    <th scope="row">
                      <Detail label={componentLabel(t, c.component)} detail={c.component} />
                    </th>
                    <td>
                      <StateWord tone={toneFor(c.status)} label={statusLabel(t, c.status)} />
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
      ) : (
        <p className="muted">{t('health_empty')}</p>
      )}
    </section>
  );
}

export interface ProvidersListProps {
  t: T;
  lang: Lang;
  state: DashboardState;
  /** True once `ai.providers` failed even after the retries in `App.tsx`. */
  failed: boolean;
  onRetry: () => void;
  disabled: boolean;
}

export function ProvidersList({ t, lang, state, failed, onRetry, disabled }: ProvidersListProps) {
  const providers = state.providers;
  return (
    <section className="sec" aria-labelledby="h-providers">
      <div className="sec-h">
        <h2 id="h-providers" className="sec-title">
          {t('providers_title')}
        </h2>
        <button type="button" className="btn btn--sm btn--plain" onClick={onRetry} disabled={disabled}>
          {t('providers_retry')}
        </button>
      </div>
      {providers && providers.length > 0 ? (
        <ul className="list list--rows">
          {providers.map((p) => {
            const reason = reasonLine(t, lang, p.reason);
            return (
              <li key={p.id} className="row">
                <span className="grow">
                  <span className="row-t">{p.displayName}</span>
                  <span className="row-d">
                    {p.local ? t('provider_local') : t('provider_cloud')}
                    {p.roles.length > 0 ? ` · ${roleList(t, p.roles)}` : ''}
                  </span>
                  {reason.text && (
                    <span className="row-d break">
                      <Detail label={reason.text} detail={reason.original} />
                    </span>
                  )}
                </span>
                <StateWord tone={toneFor(p.status)} label={statusLabel(t, p.status)} />
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="muted">{failed ? t('providers_failed') : t('providers_empty')}</p>
      )}
    </section>
  );
}

/** What the voice pipeline reports right now; Stimme › Mehr Optionen. */
export function VoiceFacts({ t, state }: { t: T; state: DashboardState }) {
  const unknown = t('unknown');
  const voice = state.voice;
  if (!voice) return <p className="muted">{unknown}</p>;
  return (
    <dl className="facts">
      <dt>{t('voice_stt')}</dt>
      <dd>{voice.sttEngine || unknown}</dd>
      <dt>{t('voice_tts')}</dt>
      <dd>{voice.ttsEngine || unknown}</dd>
      <dt>{t('voice_routing')}</dt>
      <dd>{voice.routing ? routingLabel(t, voice.routing) : unknown}</dd>
      <dt>{t('voice_latency')}</dt>
      <dd>{voice.lastLatencyMs === null ? unknown : `${voice.lastLatencyMs} ms`}</dd>
      <dt>{t('voice_listening')}</dt>
      <dd>{voice.listening ? t('capture_on') : t('capture_off')}</dd>
      <dt>{t('voice_speaking')}</dt>
      <dd>{voice.speaking ? t('capture_on') : t('capture_off')}</dd>
    </dl>
  );
}
