/**
 * The stream session and the Funken leaderboard, read once per connection for the whole app: the
 * header says "Live" from it, Heute shows the live view from it. The last known state survives a
 * failed read.
 */

import { useRefreshOnConnect } from '../hooks';
import { type IpcClient, api } from '../ipc';
import { type DashboardState, applyFunkenTop, applyStreamSessionStatus } from '../model';

/** Session status and the leaderboard, once per connection; the last known state survives a failure. */
export function useStreamRefresh(
  client: IpcClient | null,
  onState: (updater: (s: DashboardState) => DashboardState) => void,
): void {
  useRefreshOnConnect(client, async (c, cancelled) => {
    try {
      const status = await api.streamStatus(c);
      if (!cancelled()) onState((s) => applyStreamSessionStatus(s, status));
    } catch {
      /* keep the last known session */
    }
    try {
      const top = await api.funkenTop(c, 10);
      if (!cancelled()) onState((s) => applyFunkenTop(s, top));
    } catch {
      /* keep the last known leaderboard */
    }
  });
}
