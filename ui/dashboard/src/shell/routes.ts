/**
 * Where the dashboard is: one of four areas, a tab inside it, and for Settings one of eight pages.
 *
 * The route lives in the URL fragment as `#/area/sub` so the phone's back gesture and a home-screen
 * shortcut work. Fragments that are not routes (`#main` from the skip link, `#token=` before it is
 * taken out, an in-page anchor) are ignored rather than treated as "not found".
 *
 * The ten old tab ids keep working (`legacyRoute`): anything that still says "open the Remote tab"
 * lands where that content lives now. Nothing here touches the DOM.
 */

export type AreaId = 'today' | 'nox' | 'devices' | 'settings';

export const AREAS: readonly AreaId[] = ['today', 'nox', 'devices', 'settings'];

export type NoxTab = 'talk' | 'views' | 'clips' | 'history';
export const NOX_TABS: readonly NoxTab[] = ['talk', 'views', 'clips', 'history'];

export type DevicesTab = 'devices' | 'skills';
export const DEVICES_TABS: readonly DevicesTab[] = ['devices', 'skills'];

export type SettingsPageId =
  | 'privacy'
  | 'voice'
  | 'stream'
  | 'you'
  | 'nox'
  | 'home'
  | 'ai'
  | 'system';
export const SETTINGS_PAGES: readonly SettingsPageId[] = [
  'privacy',
  'voice',
  'stream',
  'you',
  'nox',
  'home',
  'ai',
  'system',
];

export type Route =
  | { area: 'today' }
  | { area: 'nox'; tab: NoxTab }
  | { area: 'devices'; tab: DevicesTab }
  /** `page: null` is the overview list. */
  | { area: 'settings'; page: SettingsPageId | null };

export const DEFAULT_ROUTE: Route = { area: 'today' };

/** The URL words. English ids in the code, short and stable; the screen shows translated names. */
const AREA_SLUG: Record<AreaId, string> = {
  today: 'today',
  nox: 'nox',
  devices: 'devices',
  settings: 'settings',
};

export function routeToHash(route: Route): string {
  switch (route.area) {
    case 'today':
      return '#/today';
    case 'nox':
      return `#/nox/${route.tab}`;
    case 'devices':
      return `#/devices/${route.tab}`;
    case 'settings':
      return route.page ? `#/settings/${route.page}` : '#/settings';
  }
}

function includes<V extends string>(list: readonly V[], value: string | undefined): value is V {
  return value !== undefined && (list as readonly string[]).includes(value);
}

/**
 * `#/area/sub` -> route; `null` for a fragment that is not a route at all (so the caller keeps
 * whatever it shows). An unknown sub-page inside a known area falls back to that area's first.
 */
export function hashToRoute(hash: string): Route | null {
  if (!hash.startsWith('#/')) return null;
  const [areaSlug, sub] = hash.slice(2).split('/');
  const area = (Object.keys(AREA_SLUG) as AreaId[]).find((a) => AREA_SLUG[a] === areaSlug);
  switch (area) {
    case 'today':
      return { area: 'today' };
    case 'nox':
      return { area: 'nox', tab: includes(NOX_TABS, sub) ? sub : 'talk' };
    case 'devices':
      return { area: 'devices', tab: includes(DEVICES_TABS, sub) ? sub : 'devices' };
    case 'settings':
      return { area: 'settings', page: includes(SETTINGS_PAGES, sub) ? sub : null };
    default:
      return legacyRoute(areaSlug ?? '') ?? DEFAULT_ROUTE;
  }
}

/** The old top-level tab ids, and where their content lives now (Spec UX-Neubau §6). */
export function legacyRoute(tab: string): Route | null {
  switch (tab) {
    case 'status':
      return { area: 'today' };
    case 'home':
      return { area: 'devices', tab: 'devices' };
    case 'chat':
      return { area: 'nox', tab: 'talk' };
    case 'board':
      return { area: 'nox', tab: 'views' };
    case 'clips':
      return { area: 'nox', tab: 'clips' };
    case 'audit':
      return { area: 'nox', tab: 'history' };
    case 'stream':
      return { area: 'today' };
    case 'remote':
      return { area: 'devices', tab: 'devices' };
    case 'presets':
    case 'capabilities':
      return { area: 'devices', tab: 'skills' };
    case 'settings':
      return { area: 'settings', page: null };
    default:
      return null;
  }
}

export function sameRoute(a: Route, b: Route): boolean {
  return routeToHash(a) === routeToHash(b);
}
