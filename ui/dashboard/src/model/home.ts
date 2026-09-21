/**
 * Smart-home view model: the `home.*` IPC payloads and the live `home.state_changed` stream.
 *
 * There is no `FieldMap` here (see `model/wire.ts`) because these payloads are not generated:
 * `home.*` is answered by the `home` plugin's tools rather than by a pydantic model in
 * `src/nox/ipc/protocol.py`, so there is no TypeScript interface to pin against. Everything is
 * therefore narrowed by hand through `shared/guards`, and nothing is invented — an entity the core
 * did not send does not appear, and a connection the core did not confirm reads as "nicht
 * verbunden" rather than as unknown-but-probably-fine.
 *
 * The list is deliberately the core's: this module never merges a state change into an entity the
 * last listing did not contain. A device that appears in Home Assistant shows up on the next
 * refresh, not through a half-known event row.
 */

import { bool, isRecord, num, numOrNull, str, stringList } from '../../../shared/guards';

/** One entity as `home.list` reports it. */
export interface HomeEntity {
  entityId: string;
  name: string;
  area: string;
  state: string;
  attributes: Record<string, unknown>;
}

/** Everything the Zuhause page knows. */
export interface HomeState {
  /** `null` until the core has answered once; `false` is a real "not connected". */
  connected: boolean | null;
  /** Why it is not connected, in the core's own words. Empty while connected. */
  reason: string;
  haVersion: string;
  host: string;
  port: number | null;
  /** Whether an access token is stored at all — the first thing setup gets wrong. */
  tokenPresent: boolean | null;
  /** False when the token may not read the area registry; rooms are then unknown, not absent. */
  areasAvailable: boolean;
  areas: string[];
  entities: HomeEntity[];
  /** True once a listing arrived, so "no devices" can be told apart from "not loaded yet". */
  loaded: boolean;
}

export const INITIAL_HOME_STATE: HomeState = {
  connected: null,
  reason: '',
  haVersion: '',
  host: '',
  port: null,
  tokenPresent: null,
  areasAvailable: false,
  areas: [],
  entities: [],
  loaded: false,
};

/** Domains the page renders a control for; everything else is shown read-only. */
export const CONTROLLABLE_DOMAINS = ['light', 'switch', 'scene'] as const;

export function domainOf(entityId: string): string {
  const index = entityId.indexOf('.');
  return index < 0 ? '' : entityId.slice(0, index);
}

/** `home.status` response. `null` when the payload is not a record at all. */
export function parseHomeStatus(payload: unknown): Partial<HomeState> | null {
  if (!isRecord(payload)) return null;
  return {
    connected: bool(payload.connected),
    reason: str(payload.reason),
    haVersion: str(payload.ha_version),
    host: str(payload.host),
    port: numOrNull(payload.port),
    tokenPresent: typeof payload.token_present === 'boolean' ? payload.token_present : null,
    areasAvailable: bool(payload.areas_available),
  };
}

function parseEntity(row: unknown): HomeEntity | null {
  if (!isRecord(row)) return null;
  const entityId = str(row.entity_id);
  if (!entityId) return null;
  return {
    entityId,
    name: str(row.name) || entityId,
    area: str(row.area),
    state: str(row.state),
    attributes: isRecord(row.attributes) ? row.attributes : {},
  };
}

/** `home.list` response. `null` when the payload has no entity array to read. */
export function parseHomeListing(payload: unknown): Partial<HomeState> | null {
  if (!isRecord(payload) || !Array.isArray(payload.entities)) return null;
  const entities: HomeEntity[] = [];
  for (const row of payload.entities) {
    const entity = parseEntity(row);
    if (entity) entities.push(entity);
  }
  return {
    connected: bool(payload.connected),
    reason: str(payload.reason),
    areas: stringList(payload.areas),
    areasAvailable: bool(payload.areas_available),
    entities,
    loaded: true,
  };
}

/** Merge a parsed partial into the slice; identical content returns the same reference. */
export function applyHome(state: HomeState, patch: Partial<HomeState> | null): HomeState {
  if (patch === null) return state;
  const next = { ...state, ...patch };
  const changed = (Object.keys(patch) as (keyof HomeState)[]).some(
    (key) => next[key] !== state[key],
  );
  return changed ? next : state;
}

/** `home.connected` event. */
export function applyHomeConnected(state: HomeState, payload: Record<string, unknown>): HomeState {
  return applyHome(state, { connected: true, reason: '', haVersion: str(payload.ha_version) });
}

/** `home.disconnected` event: the reason is the core's, not one this page writes. */
export function applyHomeDisconnected(
  state: HomeState,
  payload: Record<string, unknown>,
): HomeState {
  return applyHome(state, { connected: false, reason: str(payload.reason) });
}

/**
 * `home.state_changed` event. Only entities the last listing contained are updated: an event for
 * something unknown means the listing is stale, and the honest fix for that is a refresh, not a
 * row assembled from one event.
 */
export function applyHomeStateChanged(
  state: HomeState,
  payload: Record<string, unknown>,
): HomeState {
  const entityId = str(payload.entity_id);
  if (!entityId) return state;
  const index = state.entities.findIndex((entity) => entity.entityId === entityId);
  if (index < 0) return state;
  const current = state.entities[index];
  if (current === undefined) return state;
  const next: HomeEntity = {
    ...current,
    state: str(payload.state, current.state),
    attributes: isRecord(payload.attributes) ? payload.attributes : current.attributes,
  };
  if (next.state === current.state && next.attributes === current.attributes) return state;
  const entities = [...state.entities];
  entities[index] = next;
  return { ...state, entities };
}

/** Entities grouped by room, rooms in the order the core listed them, unassigned ones last. */
export function byArea(state: HomeState): { area: string; entities: HomeEntity[] }[] {
  const groups = new Map<string, HomeEntity[]>();
  for (const area of state.areas) groups.set(area, []);
  for (const entity of state.entities) {
    const key = entity.area || '';
    const bucket = groups.get(key);
    if (bucket) bucket.push(entity);
    else groups.set(key, [entity]);
  }
  const named = [...groups.entries()].filter(([area]) => area !== '');
  const unassigned = groups.get('') ?? [];
  const rows = named
    .filter(([, entities]) => entities.length > 0)
    .map(([area, entities]) => ({ area, entities }));
  return unassigned.length > 0 ? [...rows, { area: '', entities: unassigned }] : rows;
}

/** Brightness in percent, or `null` when the entity does not report one. */
export function brightnessPct(entity: HomeEntity): number | null {
  const raw = numOrNull(entity.attributes.brightness);
  return raw === null ? null : Math.round((num(raw, 0) / 255) * 100);
}

/** `home.command` response. */
export interface HomeCommandResult {
  matched: boolean;
  refused: boolean;
  reason: string;
  tool: string;
  summary: string;
  matchMs: number;
}

export function parseHomeCommand(payload: unknown): HomeCommandResult | null {
  if (!isRecord(payload)) return null;
  return {
    matched: bool(payload.matched),
    refused: bool(payload.refused),
    reason: str(payload.reason),
    tool: str(payload.tool),
    summary: str(payload.summary),
    matchMs: num(payload.match_ms, 0),
  };
}

/** `home.test` response: the four outcomes the Settings page distinguishes. */
export interface HomeTestResult {
  ok: boolean;
  code: string;
  detail: string;
  haVersion: string;
}

export function parseHomeTest(payload: unknown): HomeTestResult | null {
  if (!isRecord(payload)) return null;
  return {
    ok: bool(payload.ok),
    code: str(payload.code),
    detail: str(payload.detail),
    haVersion: str(payload.ha_version),
  };
}
