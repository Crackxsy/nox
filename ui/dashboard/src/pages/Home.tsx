/**
 * Zuhause: the rooms of the house, their devices and their live state (Geräte › Zuhause), plus the
 * hook the whole dashboard shares for it - Heute shows scenes and favourites, the command palette
 * matches names, and all three read the one listing `useHome` keeps in `DashboardState.home`.
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
 *
 * The free sentence ("Sag, was passieren soll") moved to the command palette, which sends it to the
 * same deterministic `home.command`.
 */

import { useCallback, useState } from 'react';

import { failureKind, reasonText } from '../../../shared/errors';
import { useRefreshOnConnect } from '../hooks';
import type { Lang, T } from '../i18n';
import { type IpcClient, api } from '../ipc';
import {
  type DashboardState,
  type HomeEntity,
  applyHome,
  brightnessPct,
  byArea,
  domainOf,
  parseHomeListing,
  parseHomeStatus,
} from '../model';
import { StateWord, type Tone } from '../ui';

/** A designed failure block rather than an error banner (see `Clips.tsx` for the same shape). */
export interface Blocked {
  title: string;
  body: string;
  detail: string | null;
}

/** What one control is doing right now, keyed by entity id. */
type Pending = Record<string, boolean>;
type Failed = Record<string, string>;

export function stateTone(entity: HomeEntity): Tone {
  if (entity.state === 'on' || entity.state === 'open' || entity.state === 'playing') return 'ok';
  return 'off';
}

export function stateLabel(t: T, entity: HomeEntity): string {
  if (entity.state === 'on') return t('home_state_on');
  if (entity.state === 'off') return t('home_state_off');
  if (!entity.state || entity.state === 'unknown' || entity.state === 'unavailable') {
    return t('home_state_unknown');
  }
  return entity.state;
}

export interface HomeController {
  blocked: Blocked | null;
  loading: boolean;
  pending: Pending;
  failed: Failed;
  refresh: () => void;
  toggle: (entity: HomeEntity) => void;
  activate: (entity: HomeEntity) => void;
}

/**
 * The listing and the controls, once for the whole app: loads on every (re)connect, and gives each
 * control its own pending and failed state.
 */
export function useHome(
  t: T,
  client: IpcClient | null,
  onState: (updater: (s: DashboardState) => DashboardState) => void,
): HomeController {
  const [blocked, setBlocked] = useState<Blocked | null>(null);
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState<Pending>({});
  const [failed, setFailed] = useState<Failed>({});

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

  const refresh = useCallback(() => {
    if (client) void load(client, () => false);
  }, [client, load]);

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
        const ok =
          typeof result === 'object' && result !== null && 'ok' in result
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

  const toggle = useCallback(
    (entity: HomeEntity) => {
      const on = entity.state !== 'on';
      const domain = domainOf(entity.entityId);
      void act(entity.entityId, (c) =>
        domain === 'light'
          ? api.homeLight(c, [entity.entityId], on)
          : api.homeSwitch(c, [entity.entityId], on),
      );
    },
    [act],
  );

  const activate = useCallback(
    (entity: HomeEntity) => {
      void act(entity.entityId, (c) => api.homeScene(c, entity.entityId));
    },
    [act],
  );

  return { blocked, loading, pending, failed, refresh, toggle, activate };
}

/** One device row: name and room, its own failure line, and the switch or the scene button. */
export function EntityRow({
  t,
  entity,
  home,
  disabled,
  showArea = false,
}: {
  t: T;
  entity: HomeEntity;
  home: HomeController;
  disabled: boolean;
  showArea?: boolean;
}) {
  const domain = domainOf(entity.entityId);
  const isPending = home.pending[entity.entityId] === true;
  const failure = home.failed[entity.entityId];
  const brightness = brightnessPct(entity);
  const where = [showArea ? entity.area : '', brightness === null ? '' : `${brightness} %`]
    .filter(Boolean)
    .join(', ');
  return (
    <li className="row">
      <span className="grow">
        {/* The entity id is technical: a tooltip, not a line (Spec §7). */}
        <span className="row-t" title={entity.entityId}>
          {entity.name}
        </span>
        {where && <span className="row-d">{where}</span>}
        {/*
          The failure belongs to this one device, so it sits in this one row - not in a banner at
          the top of the page, where it would say nothing about which switch did not move.
        */}
        {failure && (
          <span role="alert" className="row-err break">
            {failure}
          </span>
        )}
      </span>
      {domain === 'scene' ? (
        <button
          type="button"
          className="btn btn--sm"
          disabled={disabled || isPending}
          onClick={() => home.activate(entity)}
        >
          {isPending ? t('home_pending') : t('home_scene_activate')}
        </button>
      ) : domain === 'light' || domain === 'switch' ? (
        <>
          <span className="row-v" aria-hidden="true">
            {isPending ? t('home_pending') : stateLabel(t, entity)}
          </span>
          <button
            type="button"
            role="switch"
            className="switch-btn"
            aria-checked={entity.state === 'on'}
            aria-label={entity.name}
            disabled={disabled || isPending}
            onClick={() => home.toggle(entity)}
          />
        </>
      ) : (
        <StateWord tone={stateTone(entity)} label={stateLabel(t, entity)} />
      )}
    </li>
  );
}

export interface HomeRoomsProps {
  t: T;
  lang: Lang;
  state: DashboardState;
  client: IpcClient | null;
  home: HomeController;
  /** Takes the user to Settings › Zuhause, where the host and the token live. */
  onOpenSettings: () => void;
}

/** Geräte › Zuhause: every room with its devices, or what to do when Home Assistant is not there. */
export function HomeRooms({ t, state, client, home, onOpenSettings }: HomeRoomsProps) {
  const disabled = client === null;
  const rooms = byArea(state.home);
  const { blocked } = home;

  if (blocked) {
    return (
      <div className="empty">
        <div className="grow">
          <p className="row-t">{blocked.title}</p>
          <p className="muted">{blocked.body}</p>
          {blocked.detail && <p className="hint break detail-sub">{blocked.detail}</p>}
        </div>
        <button type="button" className="btn btn--sm" onClick={onOpenSettings}>
          {t('home_open_settings')}
        </button>
      </div>
    );
  }

  if (state.home.connected === false) {
    return (
      <div className="empty">
        <div className="grow">
          <p className="row-t">{t('home_disconnected')}</p>
          <p className="muted">
            {state.home.tokenPresent === false ? t('home_no_token') : t('home_disconnected_hint')}
          </p>
          {state.home.reason && <p className="hint break detail-sub">{state.home.reason}</p>}
          <dl className="facts">
            <dt>{t('setting_home_host')}</dt>
            <dd className="break">
              {state.home.host ? `${state.home.host}:${state.home.port ?? ''}` : t('home_state_unknown')}
            </dd>
          </dl>
        </div>
        <div className="btns">
          <button type="button" className="btn btn--sm" onClick={onOpenSettings}>
            {t('home_open_settings')}
          </button>
          <button type="button" className="btn btn--sm btn--plain" onClick={home.refresh} disabled={disabled}>
            {home.loading ? t('home_loading') : t('home_refresh')}
          </button>
        </div>
      </div>
    );
  }

  return (
    <>
      {/*
        Three different sentences, because "no rooms on screen" has three different causes:
        nothing has been fetched yet, Home Assistant really has nothing Nox may touch, or the
        list is there and this is just its description.
      */}
      <div className="sec-h">
        <p className="lede">
          {rooms.length > 0 ? t('home_hint') : state.home.loaded ? t('home_empty') : t('home_loading')}
        </p>
        <button type="button" className="btn btn--sm btn--plain" onClick={home.refresh} disabled={disabled}>
          {home.loading ? t('home_loading') : t('home_refresh')}
        </button>
      </div>
      {!state.home.areasAvailable && state.home.loaded && <p className="hint">{t('home_areas_unknown')}</p>}
      {rooms.map(({ area, entities }) => {
        const id = `home-area-${area ? area.replace(/[^a-zA-Z0-9]+/g, '-') : 'none'}`;
        return (
          <section key={area || 'home-no-area'} className="sub" aria-labelledby={id}>
            <h4 id={id} className="sub-h">
              {area || t('home_no_area')}
            </h4>
            <ul className="list list--rows">
              {entities.map((entity) => (
                <EntityRow key={entity.entityId} t={t} entity={entity} home={home} disabled={disabled} />
              ))}
            </ul>
          </section>
        );
      })}
      <p className="hint">{t('home_locks_note')}</p>
    </>
  );
}
