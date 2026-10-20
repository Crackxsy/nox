/**
 * Heute: what is on today and what Nox needs from the person (Spec UX-Neubau §3).
 *
 * A greeting with a one-sentence summary, then "Wartet auf dich" (open questions, facts that wait
 * for a yes, skills ready to check, parts of Nox that are down), "Dein Tag", Zuhause (scenes and a
 * few devices), Abläufe, and one line on how Nox is doing. On a desktop the page is two columns,
 * the left one wider (1.55 : 1). In the stream Modus the page becomes the live view.
 *
 * An empty Heute says what will appear here and offers three example sentences plus one action per
 * missing connection - never a blank page.
 */

import { Suspense, lazy, useState } from 'react';

import { fill, type Key, type Lang, type T, componentLabel, withCopy } from '../i18n';
import { type IpcClient, api } from '../ipc';
import {
  type DashboardState,
  type DayToday,
  type PermissionPrompt,
  type PresetRunResult,
  domainOf,
  parseRunResult,
  runOutcomeKey,
} from '../model';
import type { HomeController } from '../pages/Home';
import { EntityRow } from '../pages/Home';
import { DayTile } from '../pages/home/DayTile';
import type { PermissionPromptProps } from '../PermissionPrompt';
import type { AppData } from '../shell/useAppData';
import type { Route } from '../shell/routes';
import { troublesOf } from '../shell/presence';
import { useIpcAction } from '../hooks';

/** The live view is only needed in the stream Modus: it loads then. */
const StreamLive = lazy(() => withCopy(() => import('../pages/Stream')).then((m) => ({ default: m.StreamLive })));

const PermissionInline = lazy(() =>
  withCopy(() => import('../PermissionPrompt')).then((m) => ({ default: m.PermissionInline })),
);

const EXAMPLES: Key[] = ['ux_today_example_1', 'ux_today_example_2', 'ux_today_example_3'];

/** "Guten Morgen" until 11, "Guten Tag" until 18, then "Guten Abend". */
export function greetingKey(hour: number): Key {
  if (hour < 5) return 'ux_greet_evening';
  if (hour < 11) return 'ux_greet_morning';
  if (hour < 18) return 'ux_greet_day';
  return 'ux_greet_evening';
}

export interface TodayProps {
  t: T;
  lang: Lang;
  state: DashboardState;
  onState: (updater: (s: DashboardState) => DashboardState) => void;
  client: IpcClient | null;
  device: boolean;
  prompts: PermissionPrompt[];
  promptProps: Omit<PermissionPromptProps, 'queue' | 't' | 'client' | 'device'>;
  home: HomeController;
  data: AppData;
  navigate: (route: Route) => void;
  onAsk: (text: string) => void;
  /** Injected in tests. */
  now?: Date;
}

export function Today({
  t,
  lang,
  state,
  onState,
  client,
  device,
  prompts,
  promptProps,
  home,
  data,
  navigate,
  onAsk,
  now = new Date(),
}: TodayProps) {
  const [day, setDay] = useState<DayToday | null>(null);
  const [runs, setRuns] = useState<Record<string, PresetRunResult>>({});
  const facts = useIpcAction(client, t);
  const presetRun = useIpcAction(client, t);
  const disabled = client === null;
  const troubles = troublesOf(state);
  const scenes = state.home.entities.filter((e) => domainOf(e.entityId) === 'scene');
  const switches = state.home.entities.filter((e) => {
    const d = domainOf(e.entityId);
    return d === 'light' || d === 'switch';
  });
  // A few devices for the quick view: the ones that are on first, then the first ones listed.
  const favourites = [...switches.filter((e) => e.state === 'on'), ...switches.filter((e) => e.state !== 'on')].slice(0, 4);
  const presets = data.presets.filter((p) => p.enabled).slice(0, 4);
  const appointments = day?.events.length ?? 0;
  const waitingCount = prompts.length + data.unconfirmed.length + data.skillsReady + troubles.length;

  const greeting = data.userName
    ? `${t(greetingKey(now.getHours()))}, ${data.userName}.`
    : `${t(greetingKey(now.getHours()))}.`;

  const summary: string[] = [];
  if (day?.configured) {
    summary.push(appointments === 0 ? t('ux_sum_no_events') : fill(appointments === 1 ? t('ux_sum_event_one') : t('ux_sum_events'), appointments));
  }
  if (prompts.length > 0) summary.push(prompts.length === 1 ? t('ux_sum_question_one') : fill(t('ux_sum_questions'), prompts.length));
  if (data.unconfirmed.length > 0) summary.push(t('ux_sum_fact'));
  if (summary.length === 0) summary.push(waitingCount === 0 ? t('ux_sum_calm') : t('ux_sum_waiting'));

  const confirm = (id: number) =>
    void facts.run(`confirm:${id}`, async (c) => {
      await api.profileConfirm(c, id);
      data.dropFact(id);
    });
  const forget = (id: number) =>
    void facts.run(`forget:${id}`, async (c) => {
      await api.profileForget(c, id);
      data.dropFact(id);
    });
  const runPreset = (id: string) =>
    void presetRun.run(`run:${id}`, async (c) => {
      const result = parseRunResult(await api.presetsActivate(c, id));
      if (result) setRuns((r) => ({ ...r, [id]: result }));
    });

  const live = state.mode === 'stream';
  const nothingConnected =
    waitingCount === 0 &&
    day !== null &&
    !day.configured &&
    state.home.entities.length === 0 &&
    data.presets.length === 0;

  const waiting = (
    <section className="sec" aria-labelledby="h-waiting" id="waiting">
      <div className="sec-h">
        <h2 id="h-waiting" className="sec-title">
          {t('ux_waiting_title')}
        </h2>
        {waitingCount > 0 && <span className="count">{waitingCount}</span>}
      </div>
      {waitingCount === 0 ? (
        <p className="muted">{t('ux_waiting_none')}</p>
      ) : (
        <div className="stack">
          {prompts.length > 0 && (
            <Suspense fallback={<div className="skeleton skeleton-row" />}>
              <PermissionInline t={t} client={client} device={device} queue={prompts} idBase="today-perm" {...promptProps} />
            </Suspense>
          )}
          {data.unconfirmed.map((fact) => (
            <div key={fact.id} className="ask ask--fact">
              <div className="grow">
                <p className="ask-title-sm">{t('ux_fact_title')}</p>
                <p className="ask-what">„{fact.text}“</p>
                <p className="hint">{t('ux_fact_hint')}</p>
              </div>
              {device ? (
                <p className="hint">{t('device_local_only')}</p>
              ) : (
                <div className="btns">
                  <button type="button" className="btn btn--sm" disabled={disabled || facts.busy !== null} onClick={() => confirm(fact.id)}>
                    {t('ux_fact_keep')}
                  </button>
                  <button type="button" className="btn btn--sm btn--plain" disabled={disabled || facts.busy !== null} onClick={() => forget(fact.id)}>
                    {t('ux_fact_forget')}
                  </button>
                </div>
              )}
            </div>
          ))}
          {data.skillsReady > 0 && (
            <div className="ask ask--fact">
              <div className="grow">
                <p className="ask-title-sm">
                  {data.skillsReady === 1 ? t('ux_skill_ready_one') : fill(t('ux_skill_ready_many'), data.skillsReady)}
                </p>
                <p className="hint">{t('ux_skill_ready_hint')}</p>
              </div>
              <button type="button" className="btn btn--sm" onClick={() => navigate({ area: 'devices', tab: 'skills' })}>
                {t('ux_skill_review')}
              </button>
            </div>
          )}
          {troubles.map((component) => (
            <div key={component} className="ask ask--fact ask--trouble">
              <div className="grow">
                <p className="ask-title-sm">{fill(t('ux_trouble_title'), componentLabel(t, component))}</p>
                <p className="hint">{t('ux_trouble_hint')}</p>
              </div>
              <button type="button" className="btn btn--sm" onClick={() => navigate({ area: 'settings', page: 'system' })}>
                {t('ux_trouble_open')}
              </button>
            </div>
          ))}
          {facts.error && (
            <p role="alert" className="alert">
              {facts.error}
            </p>
          )}
        </div>
      )}
    </section>
  );

  return (
    <div className="page today">
      <header className="hello">
        <h1>{greeting}</h1>
        <p className="summary">{summary.join(' ')}</p>
      </header>

      {nothingConnected && (
        <section className="sec" aria-labelledby="h-empty">
          <h2 id="h-empty" className="sec-title">
            {t('ux_today_empty_title')}
          </h2>
          <p className="lede">{t('ux_today_empty_body')}</p>
          <div className="suggest">
            {EXAMPLES.map((key) => (
              <button key={key} type="button" className="chip" disabled={disabled} onClick={() => onAsk(t(key))}>
                {t(key)}
              </button>
            ))}
          </div>
          {!device && (
            <ul className="list list--rows">
              <li className="row">
                <span className="grow">{t('ux_connect_calendar_hint')}</span>
                <button type="button" className="btn btn--sm" onClick={() => navigate({ area: 'settings', page: 'you' })}>
                  {t('ux_connect_calendar')}
                </button>
              </li>
              <li className="row">
                <span className="grow">{t('ux_connect_home_hint')}</span>
                <button type="button" className="btn btn--sm" onClick={() => navigate({ area: 'settings', page: 'home' })}>
                  {t('ux_connect_home')}
                </button>
              </li>
            </ul>
          )}
        </section>
      )}

      {live ? (
        <>
          {waitingCount > 0 && waiting}
          <section className="sec" aria-label={t('ux_live_title')}>
            <Suspense fallback={<div className="skeleton skeleton-row" />}>
              <StreamLive t={t} lang={lang} state={state} client={client} onState={onState} />
            </Suspense>
          </section>
        </>
      ) : (
        <div className="today-grid">
          <div className="today-main">
            {waiting}
            <section className="sec">
              <DayTile
                t={t}
                lang={lang}
                client={client}
                level={2}
                onDay={setDay}
                onOpenSettings={() => navigate({ area: 'settings', page: 'you' })}
              />
            </section>
          </div>

          <div className="today-side">
            <section className="sec" aria-labelledby="h-home-quick">
              <div className="sec-h">
                <h2 id="h-home-quick" className="sec-title">
                  {t('ux_home_title')}
                </h2>
                <button type="button" className="more-link" onClick={() => navigate({ area: 'devices', tab: 'devices' })}>
                  {t('ux_home_all')}
                </button>
              </div>
              {state.home.entities.length === 0 ? (
                <p className="muted">
                  {state.home.connected === false || home.blocked ? t('ux_home_quick_off') : t('ux_home_quick_empty')}
                </p>
              ) : (
                <>
                  {scenes.length > 0 && (
                    <div className="scenes">
                      {scenes.slice(0, 6).map((scene) => (
                        <button
                          key={scene.entityId}
                          type="button"
                          className="chip"
                          disabled={disabled || home.pending[scene.entityId] === true}
                          onClick={() => home.activate(scene)}
                        >
                          {scene.name}
                        </button>
                      ))}
                    </div>
                  )}
                  <ul className="list list--rows">
                    {favourites.map((entity) => (
                      <EntityRow key={entity.entityId} t={t} entity={entity} home={home} disabled={disabled} showArea />
                    ))}
                  </ul>
                </>
              )}
            </section>

            <section className="sec" aria-labelledby="h-flows">
              <div className="sec-h">
                <h2 id="h-flows" className="sec-title">
                  {t('ux_flows_title')}
                </h2>
                <button type="button" className="more-link" onClick={() => navigate({ area: 'devices', tab: 'skills' })}>
                  {t('ux_flows_all')}
                </button>
              </div>
              {presets.length === 0 ? (
                <p className="muted">{t('ux_flows_empty')}</p>
              ) : (
                <ul className="list list--rows">
                  {presets.map((preset) => {
                    const result = runs[preset.id];
                    return (
                      <li key={preset.id} className="row">
                        <span className="grow">
                          <span className="row-t">{preset.name || preset.id}</span>
                          {preset.triggers.phrases[0] && <span className="row-d">„{preset.triggers.phrases[0]}“</span>}
                          {result && (
                            <span role="status" className={result.ok ? 'row-d ok-note' : 'row-err'}>
                              {t(runOutcomeKey(result))}
                            </span>
                          )}
                        </span>
                        {!device && (
                          <button
                            type="button"
                            className="btn btn--sm"
                            disabled={disabled || presetRun.busy !== null}
                            onClick={() => runPreset(preset.id)}
                          >
                            {presetRun.busy === `run:${preset.id}` ? t('presets_running') : t('presets_run')}
                          </button>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
              {presetRun.error && (
                <p role="alert" className="alert">
                  {presetRun.error}
                </p>
              )}
            </section>
          </div>
        </div>
      )}

      <p className="health-line">
        <span className={troubles.length === 0 ? 'dot dot--ok' : 'dot dot--warn'} aria-hidden="true" />
        {state.health === null
          ? t('ux_health_unknown')
          : troubles.length === 0
            ? t('ux_health_ok')
            : fill(troubles.length === 1 ? t('ux_health_one') : t('ux_health_many'), troubles.length)}{' '}
        <button type="button" className="more-link" onClick={() => navigate({ area: 'settings', page: 'system' })}>
          {t('ux_health_details')}
        </button>
      </p>
    </div>
  );
}
