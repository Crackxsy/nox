/**
 * The route as React state, mirrored into the URL fragment.
 *
 * `navigate` pushes a history entry (so the back gesture on a phone goes back one page) and moves
 * focus to the main region, as the spec asks after every page change - a new area or a new
 * settings page. Switching tabs inside an area is not a page change: focus stays on the tab, so
 * the arrow keys of the tab list keep working. A fragment that is not a
 * route - the skip link's `#main`, a settings anchor - leaves the route alone.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { DEFAULT_ROUTE, type Route, hashToRoute, routeToHash, sameRoute } from './routes';

function initial(): Route {
  return hashToRoute(window.location.hash) ?? DEFAULT_ROUTE;
}

export function focusMain(): void {
  const main = document.getElementById('main');
  if (main) main.focus({ preventScroll: true });
  document.documentElement.scrollTop = 0;
}

export function useRoute(): [Route, (next: Route) => void] {
  const [route, setRoute] = useState<Route>(initial);
  const current = useRef(route);
  useEffect(() => {
    current.current = route;
  }, [route]);

  useEffect(() => {
    const onHash = () => {
      const next = hashToRoute(window.location.hash);
      if (next) setRoute((current) => (sameRoute(current, next) ? current : next));
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const navigate = useCallback((next: Route) => {
    const before = current.current;
    const newPage =
      before.area !== next.area || (next.area === 'settings' && before.area === 'settings' && before.page !== next.page);
    current.current = next;
    setRoute(next);
    const hash = routeToHash(next);
    if (window.location.hash !== hash) {
      try {
        window.history.pushState(null, '', hash);
      } catch {
        window.location.hash = hash;
      }
    }
    // After the new page has rendered.
    if (newPage) window.setTimeout(focusMain, 0);
  }, []);

  return [route, navigate];
}
