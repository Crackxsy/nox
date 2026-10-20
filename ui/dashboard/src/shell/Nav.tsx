/**
 * The four areas, always in the same place (Spec UX-Neubau §3, §8): a side bar from 720 px up
 * (232 px with text on a desktop, 92 px with icon and short text on a tablet) and a 64 px bar at
 * the bottom of a phone. Both are plain navigation links with `aria-current`; Alt+1..4 reach them
 * from anywhere (App wires the keys).
 */

import type { Key, T } from '../i18n';
import { Icon, type IconName } from './icons';
import { type AreaId, AREAS, type Route, routeToHash } from './routes';

const LABEL: Record<AreaId, { long: Key; short: Key; icon: IconName }> = {
  today: { long: 'ux_area_today', short: 'ux_area_today', icon: 'today' },
  nox: { long: 'ux_area_nox', short: 'ux_area_nox', icon: 'nox' },
  devices: { long: 'ux_area_devices', short: 'ux_area_devices_short', icon: 'devices' },
  settings: { long: 'ux_area_settings', short: 'ux_area_settings', icon: 'settings' },
};

/** Where a click on an area goes: its first page. */
export function areaHome(area: AreaId): Route {
  switch (area) {
    case 'today':
      return { area: 'today' };
    case 'nox':
      return { area: 'nox', tab: 'talk' };
    case 'devices':
      return { area: 'devices', tab: 'devices' };
    case 'settings':
      return { area: 'settings', page: null };
  }
}

export function areaLabel(t: T, area: AreaId): string {
  return t(LABEL[area].long);
}

interface NavProps {
  t: T;
  route: Route;
  navigate: (route: Route) => void;
}

function NavLinks({ t, route, navigate, short }: NavProps & { short: boolean }) {
  return (
    <>
      {AREAS.map((area, i) => {
        const target = areaHome(area);
        const label = LABEL[area];
        return (
          <a
            key={area}
            href={routeToHash(target)}
            aria-current={route.area === area ? 'page' : undefined}
            aria-keyshortcuts={`Alt+${i + 1}`}
            onClick={(e) => {
              if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
              e.preventDefault();
              navigate(target);
            }}
          >
            <Icon name={label.icon} />
            <span>{t(short ? label.short : label.long)}</span>
          </a>
        );
      })}
    </>
  );
}

export function SideNav(props: NavProps) {
  const { t } = props;
  return (
    <div className="side">
      <p className="brand">
        <span className="lamp lamp--brand" aria-hidden="true" />
        {/* The product name, not translated. */}
        <span className="brand-word">Nox</span>
      </p>
      <nav aria-label={t('nav')} className="side-nav">
        <NavLinks {...props} short={false} />
      </nav>
      <p className="side-foot">
        <kbd>{t('ux_key_ctrl')}</kbd> <kbd>K</kbd> {t('ux_palette')}
      </p>
    </div>
  );
}

export function TabBar(props: NavProps) {
  return (
    <nav aria-label={props.t('ux_nav_bottom')} className="tabbar">
      <NavLinks {...props} short />
    </nav>
  );
}
