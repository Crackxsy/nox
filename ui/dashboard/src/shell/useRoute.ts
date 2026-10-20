/**
 * The route as React state, mirrored into the URL fragment.
 *
 * `navigate` pushes a history entry (so the back gesture on a phone goes back one page) and moves
 * focus to the main region, as the spec asks after every page change. A fragment that is not a
 * route - the skip link's `#main`, a settings anchor - leaves the route alone.
 */

import { useCallback, useEffect, useState } from 'react';

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

  useEffect(() => {
    const onHash = () => {
      const next = hashToRoute(window.location.hash);
      if (next) setRoute((current) => (sameRoute(current, next) ? current : next));
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const navigate = useCallback((next: Route) => {
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
    window.setTimeout(focusMain, 0);
  }, []);

  return [route, navigate];
}
