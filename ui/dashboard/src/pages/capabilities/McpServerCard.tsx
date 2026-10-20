/**
 * One external MCP server: what it is, what state it is in, what it offers, and the four things
 * the user can do with it (allow the first start, switch it on or off, remove it).
 *
 * Everything the server says about itself is untrusted: tool names and descriptions are rendered
 * as text — React escapes them — and every description carries the label "vom Server, ungeprüft".
 * A tool Nox refused stays in the list, greyed, with the reason.
 */

import { type Key, type T, fill } from '../../i18n';
import { type McpServer, type McpServerState, isKnownMcpState } from '../../model';
import { StateWord, Tile, type Tone } from '../../ui';
import { ConfirmAction } from './shared';

const STATE_KEYS: Record<McpServerState, Key> = {
  disabled: 'caps_mcp_state_disabled',
  blocked: 'caps_mcp_state_blocked',
  needs_approval: 'caps_mcp_state_needs_approval',
  starting: 'caps_mcp_state_starting',
  running: 'caps_mcp_state_running',
  failed: 'caps_mcp_state_failed',
  stopped: 'caps_mcp_state_stopped',
};

const STATE_TONES: Record<McpServerState, Tone> = {
  disabled: 'off',
  blocked: 'danger',
  needs_approval: 'warn',
  starting: 'warn',
  running: 'ok',
  failed: 'danger',
  stopped: 'off',
};

export interface McpServerCardProps {
  t: T;
  server: McpServer;
  /** Label of the action in flight on this page, or null. */
  busy: string | null;
  connected: boolean;
  onApprove: (id: string) => void;
  onEnable: (id: string, enabled: boolean) => void;
  onRemove: (id: string) => void;
}

/** The exact thing that will run: the argv as one line, quoted where a part has a space. */
function commandText(argv: string[]): string {
  return argv.map((part) => (/\s/.test(part) || part === '' ? `"${part}"` : part)).join(' ');
}

export function McpServerCard({
  t,
  server,
  busy,
  connected,
  onApprove,
  onEnable,
  onRemove,
}: McpServerCardProps) {
  const known = isKnownMcpState(server.state);
  const stateLabel = known ? t(STATE_KEYS[server.state as McpServerState]) : server.state;
  const tone: Tone = known ? STATE_TONES[server.state as McpServerState] : 'off';
  const program = server.transport === 'stdio';
  const what = program ? commandText(server.command) : server.url;
  const disabled = !connected || busy !== null;
  const domId = `mcp-${server.id}`;

  return (
    <Tile level={4} id={domId} title={server.name}>
      <p className="caps-meta">
        <StateWord tone={tone} label={stateLabel} />
        <span className="mono">{server.id}</span>
        {program && !server.sandboxed && (
          <span className="badge badge--warn">{t('caps_mcp_badge_not_sandboxed')}</span>
        )}
      </p>

      {server.reason && (
        <p className={server.state === 'failed' || server.state === 'blocked' ? 'alert' : 'hint'}>
          {fill(t('caps_mcp_reason'), server.reason)}
        </p>
      )}

      <dl className="facts">
        <dt>{t(program ? 'caps_mcp_program' : 'caps_mcp_address')}</dt>
        <dd>
          <code className="mono">{what}</code>
        </dd>
        <dt>{t('caps_mcp_version')}</dt>
        <dd>{server.version ? <code className="mono">{server.version}</code> : t('caps_mcp_version_none')}</dd>
        <dt>{t('caps_mcp_network')}</dt>
        <dd>{t(server.network ? 'caps_mcp_network_yes' : 'caps_mcp_network_no')}</dd>
        {server.secrets.length > 0 && (
          <>
            <dt>{t('caps_mcp_secrets')}</dt>
            <dd>
              {server.secrets.map((name) => (
                <code key={name} className="mono caps-block">
                  {name}
                </code>
              ))}
            </dd>
          </>
        )}
      </dl>

      {server.state === 'needs_approval' && (
        <div className="caps-approve" role="group" aria-labelledby={`${domId}-approve`}>
          <p id={`${domId}-approve`}>{t('caps_mcp_approve_lede')}</p>
          <pre className="caps-pre">
            {what}
            {'\n'}
            {fill(t('caps_mcp_approve_version'), server.version || t('caps_mcp_version_none'))}
          </pre>
          <button
            type="button"
            className="btn"
            disabled={disabled}
            onClick={() => onApprove(server.id)}
          >
            {busy === `approve:${server.id}` ? t('caps_mcp_starting') : t('caps_mcp_approve')}
          </button>
        </div>
      )}

      <details className="caps-details">
        <summary>{fill(t('caps_mcp_tools'), server.tools.length)}</summary>
        {server.tools.length === 0 ? (
          <p className="hint">{t('caps_mcp_tools_none')}</p>
        ) : (
          <ul className="caps-tools">
            {server.tools.map((tool) => (
              <li
                key={`${tool.name}|${tool.remoteName}`}
                className={tool.blocked ? 'caps-tool caps-tool--blocked' : 'caps-tool'}
              >
                <span className="caps-tool-name mono">{tool.name || tool.remoteName}</span>
                {tool.name && tool.remoteName && tool.name !== tool.remoteName && (
                  <span className="hint"> ({tool.remoteName})</span>
                )}
                {tool.readOnly && <span className="badge">{t('caps_mcp_read_only')}</span>}
                {tool.serverSaysReadOnly && !tool.readOnly && (
                  <span className="badge">{t('caps_mcp_server_says_ro')}</span>
                )}
                {tool.blocked && (
                  <p className="hint">{fill(t('caps_mcp_blocked'), tool.blocked)}</p>
                )}
                {tool.description && (
                  <p className="caps-untrusted">
                    <span className="caps-untrusted-label">{t('caps_mcp_untrusted')}</span>
                    <span className="caps-untrusted-text">{tool.description}</span>
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </details>

      <div className="tile-actions">
        {server.state !== 'needs_approval' && (
          <button
            type="button"
            className="btn btn--quiet btn--sm"
            disabled={disabled}
            onClick={() => onEnable(server.id, !server.enabled)}
          >
            {t(server.enabled ? 'caps_mcp_disable' : 'caps_mcp_enable')}
          </button>
        )}
        <ConfirmAction
          t={t}
          label={t('caps_mcp_remove')}
          question={fill(t('caps_mcp_remove_confirm'), server.name)}
          confirmLabel={t('caps_mcp_remove_yes')}
          disabled={disabled}
          onConfirm={() => onRemove(server.id)}
        />
      </div>
    </Tile>
  );
}
