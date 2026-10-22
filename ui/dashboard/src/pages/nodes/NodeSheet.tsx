/**
 * One computer in detail (Spec C §1.10): what Nox may do there, group by group, where a tool
 * family runs when nobody names a device, what Nox did there lately, and removing it.
 *
 * The switches only ever narrow. A group the device allows can be switched off here and back on
 * (with the PIN when one is set); a group the device does not allow cannot be switched on from
 * here at all - the switch then says so and shows how to do it on the device itself. The core
 * refuses it anyway (`enable_on_device`); the page never pretends otherwise.
 */

import { useCallback, useEffect, useId, useRef, useState } from 'react';

import { formatTimestamp } from '../../../../shared/format';
import { ipcReason } from '../../../../shared/errors';
import { refusalText, useIpcAction } from '../../hooks';
import { auditActionLabel, auditToolLabel, fill, type Lang, type T } from '../../i18n';
import { type IpcClient, api } from '../../ipc';
import {
  type AuditRow,
  type FamilyDefault,
  type NodeGroup,
  type NodeInfo,
  isNodeTarget,
  nodeState,
  parseFamilyDefaults,
  parseNodeGroups,
} from '../../model';
import { Sheet } from '../../shell/Sheet';
import { Icon } from '../../shell/icons';
import { StateWord } from '../../ui';
import { outcomeOf } from '../History';
import { allowCommand, defaultWords, groupLabel, stateWords } from './words';

/** How long "Wirklich entfernen" stays armed. */
export const REMOVE_DISARM_MS = 12000;
/** How many of the latest calls "Zuletzt hier getan" lists. */
export const DONE_ROWS = 5;

export interface NodeSheetProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  /** This dashboard runs on a paired device: every change needs the PIN there. */
  device: boolean;
  pinSet: boolean;
  node: NodeInfo | null;
  nodes: NodeInfo[];
  defaults: FamilyDefault[];
  audit: AuditRow[];
  onClose: () => void;
  onGroups: (nodeId: string, groups: NodeGroup[]) => void;
  onDefaults: (defaults: FamilyDefault[]) => void;
  onRemoved: (node: NodeInfo) => void;
  onOpenHistory: () => void;
}

export function NodeSheet(props: NodeSheetProps) {
  const { node, onClose } = props;
  return (
    <Sheet open={node !== null} onClose={onClose} labelledBy="node-sheet-title">
      {node && <NodeDetail {...props} node={node} />}
    </Sheet>
  );
}

const TONE = { connected: 'ok', update: 'warn', offline: 'off', removed: 'off' } as const;

function NodeDetail({
  t,
  lang,
  client,
  device,
  pinSet,
  node,
  nodes,
  defaults,
  audit,
  onClose,
  onGroups,
  onDefaults,
  onRemoved,
  onOpenHistory,
}: NodeSheetProps & { node: NodeInfo }) {
  const id = useId();
  const [pin, setPin] = useState('');
  const [explain, setExplain] = useState<string | null>(null);
  const [saved, setSaved] = useState('');
  const [armed, setArmed] = useState(false);
  /** Chosen in a select, not yet applied: group -> device. */
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const pinSentRef = useRef(false);
  const pinRef = useRef<HTMLInputElement | null>(null);
  const describe = useCallback(
    (e: unknown) => {
      const reason = ipcReason(e);
      if (reason === 'enable_on_device') return t('node_enable_title');
      if (reason === 'no_node') return t('node_reason_no_node');
      return refusalText(t, e, pinSentRef.current);
    },
    [t],
  );
  const { busy, error, setError, run } = useIpcAction(client, t, describe);
  const needsPin = device || pinSet;
  // Disarm after a while, so a stray second tap much later removes nothing.
  useEffect(() => {
    if (!armed) return;
    const timer = window.setTimeout(() => setArmed(false), REMOVE_DISARM_MS);
    return () => window.clearTimeout(timer);
  }, [armed]);
  const state = nodeState(node);
  const rows = audit
    .filter((r) => isNodeTarget(r.target, node.id))
    .slice(-DONE_ROWS)
    .reverse();

  const usedPin = (required: boolean): string => {
    const value = required ? pin.trim() : '';
    pinSentRef.current = value !== '';
    return value;
  };

  const toggle = (group: NodeGroup) => {
    if (busy !== null) return;
    setSaved('');
    // The device's word first: what it does not allow is never switched on here. A switch-off
    // made here is still cleared, so it does not linger once the device allows the group again.
    if (!group.enabledOnDevice) {
      setExplain(explain === group.group ? null : group.group);
      if (!group.narrowed) return;
    } else {
      setExplain(null);
    }
    const narrowed = !group.narrowed;
    // Switching off narrows: no PIN here (from a paired device, always one).
    const required = device || (!narrowed && pinSet);
    if (required && !pin.trim()) {
      setError(t('reason_pin_required'));
      pinRef.current?.focus();
      return;
    }
    void run(`group-${group.group}`, async (c) => {
      const answer = (await api.nodeNarrow(c, node.id, group.group, narrowed, usedPin(required))) as {
        groups?: unknown;
      };
      const groups = parseNodeGroups(answer.groups);
      if (groups) onGroups(node.id, groups);
    });
  };

  const setDefault = (entry: FamilyDefault, value: string) => {
    if (busy !== null) return;
    setSaved('');
    if (needsPin && !pin.trim()) {
      setError(t('reason_pin_required'));
      pinRef.current?.focus();
      return;
    }
    void run(`default-${entry.group}`, async (c) => {
      const answer = (await api.nodeDefaultsSet(c, entry.group, value, usedPin(needsPin))) as {
        defaults?: unknown;
      };
      const next = parseFamilyDefaults(answer.defaults);
      if (next) onDefaults(next);
      setDrafts((d) => {
        const next = { ...d };
        delete next[entry.group];
        return next;
      });
      setSaved(t('node_default_saved'));
    });
  };

  const remove = () => {
    if (!armed) {
      setArmed(true);
      setError('');
      return;
    }
    if (device && !pin.trim()) {
      setError(t('reason_pin_required'));
      pinRef.current?.focus();
      return;
    }
    setArmed(false);
    void run('remove', async (c) => {
      await api.nodeUnpair(c, node.id, usedPin(device));
      onRemoved(node);
    });
  };

  const others = nodes.filter((n) => !n.revoked);

  return (
    <>
      <div className="dlg-h">
        <h2 id="node-sheet-title">{node.name}</h2>
        <button type="button" className="ib" aria-label={t('ux_close')} onClick={onClose}>
          <Icon name="close" />
        </button>
      </div>
      <p className="node-state">
        <StateWord tone={TONE[state]} label={stateWords(t, lang, node)} />
        {node.version && <span className="muted">{fill(t('node_version'), node.version)}</span>}
      </p>
      {state === 'update' && <p className="hint">{fill(t('node_update_hint'), node.name)}</p>}
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}
      {needsPin && (
        <div className="field">
          <label htmlFor={`${id}-pin`} className="label">
            {t('node_pin')}
          </label>
          <input
            ref={pinRef}
            id={`${id}-pin`}
            className="input input--pin"
            type="password"
            inputMode="numeric"
            autoComplete="off"
            value={pin}
            onChange={(e) => setPin(e.target.value)}
            aria-describedby={`${id}-pin-hint`}
          />
          <p id={`${id}-pin-hint`} className="hint">
            {device ? t('node_pin_hint_device') : t('node_pin_hint')}
          </p>
        </div>
      )}

      <section className="node-sec" aria-labelledby={`${id}-groups`}>
        <h3 id={`${id}-groups`} className="rail-title">
          {t('node_groups_title')}
        </h3>
        <p className="hint">{t('node_groups_lede')}</p>
        {node.groups.length === 0 ? (
          <p className="muted">{t('node_may_nothing')}</p>
        ) : (
          <ul className="node-groups">
            {node.groups.map((g) => {
              const label = `${id}-g-${g.group}`;
              const note = !g.enabledOnDevice
                ? t('node_group_device_off')
                : g.narrowed
                  ? t('node_group_narrowed')
                  : t('node_group_on');
              return (
                <li key={g.group}>
                  <div className="set-row set-row--flat">
                    <span className="grow">
                      <span className="set-t" id={`${label}-l`}>
                        {groupLabel(t, g.group)}
                      </span>
                      <span className="set-d" id={`${label}-d`}>
                        {note}
                      </span>
                    </span>
                    <button
                      type="button"
                      role="switch"
                      className="switch-btn"
                      aria-checked={g.active}
                      aria-labelledby={`${label}-l`}
                      aria-describedby={`${label}-d`}
                      disabled={!client}
                      // Not `disabled` while a request runs: focus stays where it is.
                      aria-disabled={busy !== null || undefined}
                      onClick={() => toggle(g)}
                    />
                  </div>
                  {!g.enabledOnDevice && (
                    // Always in the page, so a screen reader hears it when it fills.
                    <div role="status" className={explain === g.group ? 'node-how' : undefined}>
                      {explain === g.group && (
                        <>
                          <p className="set-t">{t('node_enable_title')}</p>
                          <p className="hint">{t('node_enable_how')}</p>
                          <p className="mono break">{allowCommand(t, g.group)}</p>
                          <p className="hint">{t('node_enable_tray')}</p>
                        </>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {defaults.length > 0 && (
        <section className="node-sec" aria-labelledby={`${id}-defaults`}>
          <h3 id={`${id}-defaults`} className="rail-title">
            {t('node_defaults_title')}
          </h3>
          <p className="hint">{t('node_defaults_lede')}</p>
          {defaults.map((entry) => {
            const field = `${id}-d-${entry.group}`;
            const known =
              ['auto', 'core', 'origin'].includes(entry.device) || others.some((n) => n.id === entry.device);
            const current = known ? entry.device : '';
            const draft = drafts[entry.group] ?? current;
            return (
              <div key={entry.group} className="field node-default">
                <label htmlFor={field} className="label">
                  {fill(t('node_default_select'), groupLabel(t, entry.group))}
                </label>
                <div className="row-controls">
                  <select
                    id={field}
                    className="select"
                    value={draft}
                    disabled={!client}
                    aria-disabled={busy !== null || undefined}
                    onChange={(e) => {
                      const value = e.target.value;
                      if (busy === null) setDrafts((d) => ({ ...d, [entry.group]: value }));
                    }}
                  >
                    {!known && (
                      <option value="" disabled>
                        {defaultWords(t, entry, nodes)}
                      </option>
                    )}
                    <option value="auto">{t('node_default_auto')}</option>
                    <option value="origin">{t('node_default_origin')}</option>
                    <option value="core">{t('node_default_core')}</option>
                    {others.map((n) => (
                      <option key={n.id} value={n.id}>
                        {n.name}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className="btn btn--sm"
                    disabled={!client || draft === current || draft === ''}
                    aria-disabled={busy !== null || undefined}
                    aria-label={`${t('node_default_apply')}: ${groupLabel(t, entry.group)}`}
                    onClick={() => setDefault(entry, draft)}
                  >
                    {t('node_default_apply')}
                  </button>
                </div>
              </div>
            );
          })}
          <p role="status" className={saved ? 'hint' : 'sr-only'}>
            {saved}
          </p>
        </section>
      )}

      <section className="node-sec" aria-labelledby={`${id}-done`}>
        <h3 id={`${id}-done`} className="rail-title">
          {t('node_done_title')}
        </h3>
        {rows.length === 0 ? (
          <p className="muted">{t('node_done_empty')}</p>
        ) : (
          <ul className="list list--rows">
            {rows.map((row) => (
              <li key={row.seq} className="row">
                <span className="grow">
                  <span className="row-t">
                    {auditToolLabel(t, row.tool)} – {auditActionLabel(t, row.action)}
                  </span>
                  <span className="row-d">
                    <span title={row.ts}>{formatTimestamp(row.ts, lang)}</span>
                  </span>
                </span>
                <span className={outcomeOf(row) === 'allowed' ? 'badge badge--ok' : 'badge'}>
                  {t(OUTCOME_KEY[outcomeOf(row)])}
                </span>
              </li>
            ))}
          </ul>
        )}
        <button type="button" className="btn btn--quiet btn--sm" onClick={onOpenHistory}>
          {t('node_done_all')}
        </button>
      </section>

      <details className="tile-technical">
        <summary>{t('lan_technical')}</summary>
        <dl className="tech-list">
          <dt>{t('node_tech_id')}</dt>
          <dd className="mono break">{node.id}</dd>
          <dt>{t('node_tech_system')}</dt>
          <dd className="mono">{[node.os, node.arch].filter(Boolean).join(' · ') || t('unknown')}</dd>
          <dt>{t('remote_col_paired')}</dt>
          <dd>{formatTimestamp(node.pairedAt, lang)}</dd>
          <dt>{t('node_tech_tools')}</dt>
          <dd className="mono break">{node.tools.length ? node.tools.join(', ') : t('node_tech_none')}</dd>
        </dl>
      </details>

      <div className="node-remove">
        <button
          type="button"
          className={armed ? 'btn btn--sm btn--armed' : 'btn btn--sm btn--quiet'}
          disabled={!client || busy === 'remove'}
          onClick={remove}
          aria-describedby={armed ? `${id}-remove` : undefined}
        >
          {armed ? t('lan_remove_armed') : t('lan_remove')}
        </button>
        {armed && (
          <p id={`${id}-remove`} role="status" className="hint">
            {fill(t('node_remove_confirm'), node.name)}
          </p>
        )}
      </div>
    </>
  );
}

const OUTCOME_KEY = {
  allowed: 'ux_hist_outcome_allowed',
  denied: 'ux_hist_outcome_denied',
  failed: 'ux_hist_outcome_failed',
  asked: 'ux_hist_outcome_asked',
} as const;
