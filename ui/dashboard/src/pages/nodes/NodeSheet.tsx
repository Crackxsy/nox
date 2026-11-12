/**
 * One computer in detail (Spec C §1.10): what Nox may do there, group by group, what Nox did
 * there lately, and removing it. Where a tool family runs when nobody names a device applies to
 * every computer and lives under "Standardgeräte" on the page itself (`DefaultsSection`).
 *
 * The switches only ever narrow. A group the device allows is a switch - the whole row - that
 * turns it off here and back on (with the PIN when one is set). A group the device does not allow
 * is no switch at all: a lock, and "So schaltest du es ein" opens how to do it on that device, in
 * the words of its system (the Windows Command Prompt, a terminal, `sudo` on a Pi). The core
 * refuses to widen anyway (`enable_on_device`); the page never pretends otherwise.
 *
 * From a paired phone or tablet every change needs the security PIN; with none set there is no
 * PIN field, only where it can be done instead.
 */

import { useCallback, useEffect, useId, useRef, useState } from 'react';

import { formatTimestamp } from '../../../../shared/format';
import { ipcReason } from '../../../../shared/errors';
import { refusalText, useIpcAction } from '../../hooks';
import { auditActionLabel, auditToolLabel, fill, type Lang, type T } from '../../i18n';
import { type IpcClient, api } from '../../ipc';
import {
  type AuditRow,
  type NodeGroup,
  type NodeInfo,
  isNodeTarget,
  nodeState,
  parseNodeGroups,
  shortFolder,
} from '../../model';
import { Sheet } from '../../shell/Sheet';
import { Icon } from '../../shell/icons';
import { Filled, StateWord } from '../../ui';
import { outcomeOf } from '../History';
import { allowCommand, enableHowKey, groupLabel, mayBeHeadless, stateWords } from './words';

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
  audit: AuditRow[];
  onClose: () => void;
  onGroups: (nodeId: string, groups: NodeGroup[]) => void;
  onRemoved: (node: NodeInfo) => void;
  onOpenHistory: () => void;
}

export function NodeSheet(props: NodeSheetProps) {
  const { node, onClose } = props;
  return (
    <Sheet open={node !== null} onClose={onClose} labelledBy="node-sheet-title" className="node-sheet">
      {node && <NodeDetail {...props} node={node} />}
    </Sheet>
  );
}

const TONE = { connected: 'ok', listing: 'off', update: 'warn', offline: 'off', removed: 'off' } as const;

/** What a group's row says under its name. */
function groupNote(t: T, node: NodeInfo, g: NodeGroup): string {
  if (!g.enabledOnDevice) return t('node_group_device_off');
  if (g.narrowed) return g.group === 'voice' ? t('node_voice_off_here') : t('node_group_narrowed');
  if (g.group === 'files' && node.folders !== null) {
    if (node.folders.length === 0) return t('node_group_no_folder');
    const shown = node.folders.map(shortFolder).join(', ');
    return fill(t(node.folders.length === 1 ? 'node_group_on_folder' : 'node_group_on_folders'), shown);
  }
  return t('node_group_on');
}

function NodeDetail({
  t,
  lang,
  client,
  device,
  pinSet,
  node,
  audit,
  onClose,
  onGroups,
  onRemoved,
  onOpenHistory,
}: NodeSheetProps & { node: NodeInfo }) {
  const id = useId();
  const [pin, setPin] = useState('');
  const [explain, setExplain] = useState<string | null>(null);
  // Switching voice on here makes that device's microphone an input for Nox: said, then confirmed.
  const [voiceAsk, setVoiceAsk] = useState(false);
  const [armed, setArmed] = useState(false);
  const pinSentRef = useRef(false);
  const pinRef = useRef<HTMLInputElement | null>(null);
  const describe = useCallback(
    (e: unknown) => {
      const reason = ipcReason(e);
      if (reason === 'enable_on_device') return t('node_enable_title');
      if (reason === 'no_node') return t('node_reason_no_node');
      // Never the core's English sentence in this sheet.
      return refusalText(t, e, pinSentRef.current, t('node_change_failed'));
    },
    [t],
  );
  const { busy, error, setError, run } = useIpcAction(client, t, describe);
  /** A paired device without a PIN set can change nothing here: no field, only where instead. */
  const locked = device && !pinSet;
  const needsPin = (device || pinSet) && !locked;
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

  const narrow = (group: NodeGroup, narrowed: boolean, quiet = false) => {
    // Switching off narrows: no PIN here (from a paired device, always one).
    const required = device || (!narrowed && pinSet);
    if (required && !pin.trim()) {
      if (quiet) return;
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

  const toggle = (group: NodeGroup, confirmed = false) => {
    if (busy !== null || locked) return;
    if (group.group === 'voice' && group.narrowed && !confirmed) {
      setVoiceAsk(!voiceAsk);
      return;
    }
    setVoiceAsk(false);
    setExplain(null);
    narrow(group, !group.narrowed);
  };

  const showHow = (group: NodeGroup) => {
    const opening = explain !== group.group;
    setExplain(opening ? group.group : null);
    // A switch-off made here is cleared on the way, so it does not linger once the device allows
    // the group (never widening: the device still has it off). Only when no PIN is needed for it.
    if (opening && group.narrowed && busy === null && !locked) narrow(group, false, true);
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
      {locked && <p className="hint">{t('node_pin_missing_device')}</p>}
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
              const how = `${label}-how`;
              const open = explain === g.group;
              return (
                <li key={g.group}>
                  {g.enabledOnDevice ? (
                    // The whole row is the switch: a target as wide as the sheet, one tap.
                    <button
                      type="button"
                      role="switch"
                      className="set-row set-row--flat set-row--switch"
                      aria-checked={g.active}
                      aria-labelledby={`${label}-l`}
                      aria-describedby={`${label}-d`}
                      disabled={!client || locked}
                      // Not `disabled` while a request runs: focus stays where it is.
                      aria-disabled={busy !== null || undefined}
                      onClick={() => toggle(g)}
                    >
                      <span className="grow">
                        <span className="set-t" id={`${label}-l`}>
                          {groupLabel(t, g.group)}
                        </span>
                        <span className="set-d" id={`${label}-d`}>
                          {groupNote(t, node, g)}
                        </span>
                      </span>
                      <span className="switch-btn" data-on={g.active} aria-hidden="true" />
                    </button>
                  ) : (
                    <div className="set-row set-row--flat set-row--locked">
                      <span className="node-lock" aria-hidden="true">
                        <Icon name="lock" size={18} />
                      </span>
                      <span className="grow">
                        <span className="set-t" id={`${label}-l`}>
                          {groupLabel(t, g.group)}
                        </span>
                        <span className="set-d" id={`${label}-d`}>
                          {groupNote(t, node, g)}
                        </span>
                      </span>
                      <button
                        type="button"
                        className="btn btn--sm btn--quiet"
                        aria-expanded={open}
                        aria-controls={how}
                        aria-describedby={`${label}-l`}
                        onClick={() => showHow(g)}
                      >
                        {t('node_enable_show')}
                      </button>
                    </div>
                  )}
                  {g.group === 'voice' && (
                    // Always in the page, so a screen reader hears it when it fills.
                    <div role="status" className={voiceAsk ? 'node-how' : undefined}>
                      {voiceAsk && (
                        <>
                          <p className="set-t">{t('node_voice_confirm_title')}</p>
                          <p className="hint">{t('node_voice_confirm_text')}</p>
                          <p className="hint">{t('node_voice_two_keys')}</p>
                          <button
                            type="button"
                            className="btn"
                            data-testid="node-voice-confirm"
                            onClick={() => toggle(g, true)}
                          >
                            {t('node_voice_confirm_go')}
                          </button>
                        </>
                      )}
                    </div>
                  )}
                  {!g.enabledOnDevice && open && (
                    <div id={how} className="node-how">
                      <p className="set-t">{t('node_enable_title')}</p>
                      <p className="hint">
                        <Filled pattern={t(enableHowKey(node))} args={[node.name]} strong={[0]} />
                      </p>
                      <p className="mono break">{allowCommand(t, g.group, node.headless === true)}</p>
                      {mayBeHeadless(node) && (
                        <>
                          <p className="hint">{fill(t('node_enable_headless_hint'), node.name)}</p>
                          <p className="mono break">{allowCommand(t, g.group, true)}</p>
                        </>
                      )}
                      <p className="hint">{t('node_enable_after')}</p>
                      {node.headless !== true && <p className="hint">{t('node_enable_tray')}</p>}
                      {g.group === 'voice' && <p className="hint">{t('node_voice_two_keys')}</p>}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

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
          {node.folders !== null && (
            <>
              <dt>{t('node_tech_folders')}</dt>
              <dd className="mono break">{node.folders.length ? node.folders.join(', ') : t('node_tech_none')}</dd>
            </>
          )}
        </dl>
      </details>

      <div className="node-remove">
        <button
          type="button"
          className={armed ? 'btn btn--sm btn--danger' : 'btn btn--sm btn--quiet'}
          disabled={!client || busy === 'remove' || locked}
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
