/**
 * The MCP section of "Fähigkeiten": the configured servers, the add form, and the note that local
 * servers run unisolated until the sandbox is installed.
 *
 * After every write the list is read back, so the card shows the state the core reached — a
 * server that was just approved says "läuft" or "fehlgeschlagen", never an optimistic guess. A
 * refusal (`ok: false`) is shown with the core's reason, verbatim.
 */

import { useCallback, useState } from 'react';

import { useIpcAction } from '../../hooks';
import { type T, fill } from '../../i18n';
import { type IpcClient, api } from '../../ipc';
import { type McpServerList, parseActionResult, parseMcpServers } from '../../model';
import { Tile } from '../../ui';
import { McpAddForm } from './McpAddForm';
import { McpServerCard } from './McpServerCard';
import { loadError, useReload } from './shared';

export interface McpSectionProps {
  t: T;
  client: IpcClient | null;
  revision: number;
  pinConfigured: boolean;
}

export function McpSection({ t, client, revision, pinConfigured }: McpSectionProps) {
  const [list, setList] = useState<McpServerList | null>(null);
  const [loadFailed, setLoadFailed] = useState('');
  const [adding, setAdding] = useState(false);
  const [refusal, setRefusal] = useState('');
  const [notice, setNotice] = useState('');
  const action = useIpcAction(client, t);

  const load = useCallback(
    async (c: IpcClient, cancelled: () => boolean = () => false) => {
      try {
        const parsed = parseMcpServers(await api.mcpServers(c));
        if (cancelled()) return;
        setList(parsed);
        setLoadFailed('');
      } catch (e) {
        if (!cancelled()) setLoadFailed(loadError(t, e));
      }
    },
    [t],
  );
  useReload(client, revision, load);

  /** One write, then a fresh list; `ok: false` becomes the visible refusal. */
  const write = (label: string, fn: (c: IpcClient) => Promise<unknown>) => {
    setRefusal('');
    setNotice('');
    void action.run(label, async (c) => {
      const result = parseActionResult(await fn(c));
      if (!result.ok) setRefusal(fill(t('caps_refused'), result.reason || t('unknown')));
      await load(c);
    });
  };

  const servers = list?.servers ?? [];

  return (
    <section className="caps-section" aria-labelledby="h-caps-mcp">
      <div className="rail-head">
        <div>
          <h3 id="h-caps-mcp" className="rail-title">
            {t('caps_mcp_title')}
          </h3>
          <p className="rail-sub">{t('caps_mcp_lede')}</p>
        </div>
      </div>

      {loadFailed && (
        <p role="alert" className="alert">
          {loadFailed}
        </p>
      )}
      {list && !list.sandboxed && <p className="hint caps-emph">{t('caps_mcp_not_sandboxed')}</p>}
      {refusal && (
        <p role="alert" className="alert">
          {refusal}
        </p>
      )}
      {action.error && (
        <p role="alert" className="alert">
          {action.error}
        </p>
      )}
      {notice && (
        <p role="status" className="ok-note">
          {notice}
        </p>
      )}

      <div className="tiles tiles--single">
        {list && servers.length === 0 && !adding && (
          <Tile level={4} title={t('caps_mcp_empty')}>
            <p className="muted">{t('caps_mcp_empty_hint')}</p>
          </Tile>
        )}

        {servers.map((server) => (
          <McpServerCard
            key={server.id}
            t={t}
            server={server}
            busy={action.busy}
            connected={client !== null}
            onApprove={(id) => write(`approve:${id}`, (c) => api.mcpServerApprove(c, id))}
            onEnable={(id, enabled) => write(`enable:${id}`, (c) => api.mcpServerEnable(c, id, enabled))}
            onRemove={(id) => write(`remove:${id}`, (c) => api.mcpServerRemove(c, id))}
          />
        ))}

        {adding && (
          <div className="tile">
            <McpAddForm
              t={t}
              client={client}
              takenIds={servers.map((server) => server.id)}
              pinConfigured={pinConfigured}
              onAdded={(id) => {
                setAdding(false);
                setNotice(fill(t('caps_add_done'), id));
                if (client) void load(client);
              }}
              onCancel={() => setAdding(false)}
            />
          </div>
        )}
      </div>

      {!adding && (
        <div className="tile-actions">
          <button
            type="button"
            className="btn"
            disabled={client === null || list === null}
            onClick={() => {
              setNotice('');
              setAdding(true);
            }}
          >
            {t('caps_add_open')}
          </button>
        </div>
      )}
    </section>
  );
}
