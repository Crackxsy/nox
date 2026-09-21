/**
 * Zuhause: the rooms of the house, their devices and their live state.
 *
 * The page is built around one honesty rule that a smart-home screen gets wrong more often than
 * any other: a toggle must never look as if it worked. Every control therefore carries its own
 * pending and failed state — the switch that is in flight shows it, and a switch Home Assistant
 * refused says so next to itself, not in a banner at the top where it belongs to nothing.
 *
 * State comes from two places and they are kept apart on purpose. `home.list` is the core's list
 * and the only thing that decides which devices exist; `home.state_changed` events update the
 * state of rows that list already contains. An event for an unknown entity means the list is
 * stale, and the fix for that is the refresh link, not a row assembled from half a payload.
 *
 * Locks, alarm panels, garage doors and valves are absent by construction — the core never sends
 * them — and the page says so in one line rather than leaving a stranger to wonder.
 */

import { useCallback, useState } from 'react';

import { failureKind, reasonText } from '../../../shared/errors';
import { useIpcAction, useRefreshOnConnect } from '../hooks';
import { type Lang, type T, fill } from '../i18n';
import { type IpcClient, api } from '../ipc';
import {
  type DashboardState,
  type HomeCommandResult,
  type HomeEntity,
  applyHome,
  brightnessPct,
  byArea,
  domainOf,
  parseHomeCommand,
  parseHomeListing,
  parseHomeStatus,
} from '../model';
import { Detail, Hero, StateWord, Tile, type Tone } from '../ui';

export interface HomePageProps {
  t: T;
  lang: Lang;
  state: DashboardState;
  /** null while offline: every request would fail, so the controls are disabled instead. */
  client: IpcClient | null;
  onState: (updater: (s: DashboardState) => DashboardState) => void;
  /** Takes the user to Settings → Zuhause, where the host and the token live. */
  onOpenSettings: () => void;
}

/** A designed failure tile rather than an error banner (see `Clips.tsx` for the same shape). */
interface Blocked {
  title: string;
  body: string;
  detail: string | null;
}

/** What one control is doing right now, keyed by entity id. */
type Pending = Record<string, boolean>;
type Failed = Record<string, string>;

function stateTone(entity: HomeEntity): Tone {
  if (entity.state === 'on' || entity.state === 'open' || entity.state === 'playing') return 'ok';
  if (entity.state === 'unavailable' || entity.state === 'unknown') return 'off';
  return 'off';
}

function stateLabel(t: T, entity: HomeEntity): string {
  if (entity.state === 'on') return t('home_state_on');
  if (entity.state === 'off') return t('home_state_off');
  if (!entity.state || entity.state === 'unknown' || entity.state === 'unavailable') {
    return t('home_state_unknown');
  }
  return entity.state;
}

export function HomePage({ t, lang, state, client, onState, onOpenSettings }: HomePageProps) {
  const [blocked, setBlocked] = useState<Blocked | null>(null);
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState<Pending>({});
  const [failed, setFailed] = useState<Failed>({});
  const [draft, setDraft] = useState('');
  const [command, setCommand] = useState<HomeCommandResult | null>(null);
  const { busy, error, run } = useIpcAction(client, t);
  const home = state.home;
  const disabled = client === null;

  const load = useCallback(
    async (c: IpcClient, cancelled: () => boolean) => {
      setLoading(true);
      try {
        const [status, listing] = await Promise.all([api.homeStatus(c), api.homeList(c)]);
        if (cancelled()) return;
        setBlocked(null);
        onState((s) => ({
          ...s,
          home: applyHome(applyHome(s.home, parseHomeStatus(status)), parseHomeListing(listing)),
        }));
      } catch (e) {
        if (cancelled()) return;
        const refused = failureKind(e) === 'refused';
        setBlocked({
          title: refused ? t('home_refused') : t('home_disconnected'),
          body: refused ? t('home_refused_hint') : t('home_disconnected_hint'),
          detail: reasonText(e) || null,
        });
      } finally {
        if (!cancelled()) setLoading(false);
      }
    },
    [onState, t],
  );

  useRefreshOnConnect(client, load);

  const refresh = () => {
    if (client) void load(client, () => false);
  };

  /** One control's own lifecycle: pending while in flight, its own failure line when it fails. */
  const act = useCallback(
    async (entityId: string, call: (c: IpcClient) => Promise<unknown>) => {
      if (!client) return;
      setPending((p) => ({ ...p, [entityId]: true }));
      setFailed((f) => {
        const { [entityId]: _removed, ...rest } = f;
        return rest;
      });
      try {
        const result = await call(client);
        const ok = typeof result === 'object' && result !== null && 'ok' in result
          ? (result as { ok: unknown }).ok === true
          : true;
        if (!ok) {
          const reason =
            typeof result === 'object' && result !== null && 'reason' in result
              ? String((result as { reason: unknown }).reason)
              : '';
          setFailed((f) => ({ ...f, [entityId]: reason || t('home_failed') }));
        }
      } catch (e) {
        setFailed((f) => ({ ...f, [entityId]: reasonText(e) || t('home_failed') }));
      } finally {
        setPending((p) => {
          const { [entityId]: _removed, ...rest } = p;
          return rest;
        });
      }
    },
    [client, t],
  );

  const toggle = (entity: HomeEntity) => {
    const on = entity.state !== 'on';
    const domain = domainOf(entity.entityId);
    void act(entity.entityId, (c) =>
      domain === 'light'
        ? api.homeLight(c, [entity.entityId], on)
        : api.homeSwitch(c, [entity.entityId], on),
    );
  };

  const activate = (entity: HomeEntity) => {
    void act(entity.entityId, (c) => api.homeScene(c, entity.entityId));
  };

  const send = () => {
    const text = draft.trim();
    if (!text) return;
    void run('home-command', async (c) => {
      const result = parseHomeCommand(await api.homeCommand(c, text));
      setCommand(result);
      if (result?.matched) {
        setDraft('');
        await load(c, () => false);
      }
    });
  };

  const rooms = byArea(home);

  return (
    <div className="page wrap">
      <Hero
        title={t('hero_home')}
        sub={t('hero_home_sub')}
        links={
          <button type="button" className="link" onClick={onOpenSettings}>
            {t('home_open_settings')}
          </button>
        }
      />

      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      {blocked ? (
        <div className="tiles tiles--single">
          <Tile id="home-blocked" title={blocked.title} lede={blocked.body}>
            {blocked.detail && <p className="hint break detail-sub">{blocked.detail}</p>}
            <div className="tile-actions">
              <button type="button" className="btn btn--sm" onClick={onOpenSettings}>
                {t('home_open_settings')}
              </button>
            </div>
          </Tile>
        </div>
      ) : home.connected === false ? (
        <div className="tiles tiles--single">
          <Tile
            id="home-offline"
            title={t('home_disconnected')}
            lede={home.tokenPresent === false ? t('home_no_token') : t('home_disconnected_hint')}
          >
            {home.reason && <p className="hint break detail-sub">{home.reason}</p>}
            <dl className="facts">
              <dt>{t('setting_home_host')}</dt>
              <dd className="break">
                {home.host ? `${home.host}:${home.port ?? ''}` : t('home_state_unknown')}
              </dd>
            </dl>
            <div className="tile-actions">
              <button type="button" className="btn btn--sm" onClick={onOpenSettings}>
                {t('home_open_settings')}
              </button>
              <button type="button" className="link" onClick={refresh} disabled={disabled}>
                {loading ? t('home_loading') : t('home_refresh')}
              </button>
            </div>
          </Tile>
        </div>
      ) : (
        <>
          <div className="tiles tiles--single">
            <Tile
              feature
              id="home-command"
              title={t('home_command_title')}
              lede={t('home_command_hint')}
            >
              <div className="row-controls">
                <div className="field field--grow">
                  <label htmlFor="home-command-input" className="label">
                    {t('home_command_label')}
                  </label>
                  <input
                    id="home-command-input"
                    type="text"
                    className="input"
                    autoComplete="off"
                    placeholder={t('home_command_placeholder')}
                    value={draft}
                    disabled={disabled}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') send();
                    }}
                  />
                </div>
                <button
                  type="button"
                  className="btn"
                  disabled={disabled || busy === 'home-command' || draft.trim() === ''}
                  onClick={send}
                >
                  {t('home_command_send')}
                </button>
              </div>
              <p role="status" aria-live="polite" className="hint break">
                {command === null
                  ? ''
                  : command.refused
                    ? `${t('home_command_refused')} ${command.reason}`
                    : !command.matched
                      ? t('home_command_unmatched')
                      : `${t('home_command_done')}: ${command.summary} — ${fill(
                          t('home_command_latency'),
                          command.matchMs.toLocaleString(lang, { maximumFractionDigits: 1 }),
                        )}`}
              </p>
            </Tile>
          </div>

          <section aria-labelledby="h-home">
            <div className="rail-head">
              <div>
                <h3 id="h-home" className="rail-title">
                  {t('home_title')}
                </h3>
                {/*
                  Three different sentences, because "no rooms on screen" has three different
                  causes: nothing has been fetched yet, Home Assistant really has nothing Nox may
                  touch, or the list is there and this is just its description.
                */}
                <p className="rail-sub">
                  {rooms.length > 0
                    ? t('home_hint')
                    : home.loaded
                      ? t('home_empty')
                      : t('home_loading')}
                </p>
              </div>
              <button type="button" className="link" onClick={refresh} disabled={disabled}>
                {loading ? t('home_loading') : t('home_refresh')}
              </button>
            </div>

            {!home.areasAvailable && home.loaded && (
              <p className="hint">{t('home_areas_unknown')}</p>
            )}

            <div className="tiles">
              {rooms.map(({ area, entities }) => (
                <Tile
                  key={area || 'home-no-area'}
                  level={4}
                  id={`home-area-${area ? area.replace(/[^a-zA-Z0-9]+/g, '-') : 'none'}`}
                  title={area || t('home_no_area')}
                >
                  <ul className="list">
                    {entities.map((entity) => {
                      const domain = domainOf(entity.entityId);
                      const isPending = pending[entity.entityId] === true;
                      const failure = failed[entity.entityId];
                      const brightness = brightnessPct(entity);
                      return (
                        <li key={entity.entityId}>
                          <span className="list-main">
                            <Detail
                              label={entity.name}
                              detail={
                                brightness === null
                                  ? entity.entityId
                                  : `${brightness} % · ${entity.entityId}`
                              }
                            />
                            {/*
                              The failure belongs to this one device, so it sits in this one row's
                              column — not in a banner at the top of the page, where it would say
                              nothing about which switch did not move.
                            */}
                            {failure && (
                              <span role="alert" className="hint break">
                                {failure}
                              </span>
                            )}
                          </span>
                          <span className="list-meta">
                            <StateWord tone={stateTone(entity)} label={stateLabel(t, entity)} />
                            {domain === 'scene' ? (
                              <button
                                type="button"
                                className="btn btn--sm"
                                disabled={disabled || isPending}
                                onClick={() => activate(entity)}
                              >
                                {isPending ? t('home_pending') : t('home_scene_activate')}
                              </button>
                            ) : domain === 'light' || domain === 'switch' ? (
                              <button
                                type="button"
                                className="btn btn--sm"
                                disabled={disabled || isPending}
                                aria-pressed={entity.state === 'on'}
                                onClick={() => toggle(entity)}
                              >
                                {isPending
                                  ? t('home_pending')
                                  : entity.state === 'on'
                                    ? t('home_turn_off')
                                    : t('home_turn_on')}
                              </button>
                            ) : null}
                          </span>
                        </li>
                      );
                    })}
                  </ul>
                </Tile>
              ))}
            </div>

            <p className="hint">{t('home_locks_note')}</p>
          </section>
        </>
      )}
    </div>
  );
}
