/**
 * Stopp is always reachable (Spec UX-Neubau §5, owner decision): not only in the header, which a
 * modal surface makes inert, but inside every modal surface too - the menus, the command line,
 * the permission sheet and the first run. Each gets its own Stopp at the header's spot (or, in
 * the command line, in its top row); all of them send the same `security.kill` through
 * `StopContext`, which `App` provides.
 */

import { createContext, useContext } from 'react';

import type { T } from '../i18n';
import { Icon } from './icons';

export interface StopApi {
  t: T;
  stop: () => void;
  /** A stop is on its way. */
  busy: boolean;
  /** There is a connection to send it on. */
  canStop: boolean;
  /** Nox is stopped already: no Stopp is shown, the strip offers Fortsetzen. */
  stopped: boolean;
}

export const StopContext = createContext<StopApi | null>(null);

export interface StopButtonProps {
  /** `corner`: fixed at the header's Stopp spot, above the surface; `inline`: in the flow. */
  placement: 'corner' | 'inline';
}

export function StopButton({ placement }: StopButtonProps) {
  const api = useContext(StopContext);
  if (!api || api.stopped) return null;
  return (
    <button
      type="button"
      className={placement === 'corner' ? 'stop stop--corner' : 'stop stop--inline'}
      onClick={api.stop}
      disabled={!api.canStop || api.busy}
      aria-keyshortcuts="Control+Alt+Pause"
      title={api.t('ux_stop_desc')}
    >
      <Icon name="stop" size={14} />
      {api.t('ux_stop')}
    </button>
  );
}
