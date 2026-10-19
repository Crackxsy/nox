/**
 * Whether this dashboard runs on a paired device rather than on the machine Nox runs on.
 *
 * Administration - settings, privacy, the mode, presets, clips, what Nox remembers - is refused to
 * a paired device by the core (`nox.ipc.roles`). The pages read this to not offer such controls
 * there in the first place, and to say where they work instead.
 */

import { createContext, useContext } from 'react';

export const DeviceContext = createContext(false);

export function useDevice(): boolean {
  return useContext(DeviceContext);
}
