/**
 * Geräte › Computer und Server (Spec C §1.10): every PC, laptop and small server paired as a
 * node, above the phones. One row per node - its kind, name, state ("Verbunden", "Offline seit
 * 14:02", "Update nötig"), in one sentence what Nox may do there, whether it is the default
 * device for something, its version - and a detail sheet per node (`nodes/NodeSheet.tsx`).
 *
 * Honest about the state like the phones section: loading, an answer this page cannot read, a
 * request that failed, network access off. A core without nodes at all (an older version: it
 * does not know `node.list`) shows nothing, so the phones section stays as it was.
 *
 * The list stays current by itself: on `remote.paired` / `remote.revoked`, every 30 seconds while
 * the page is visible, and every 3 seconds while a code for a new computer is out.
 */

import { useCallback, useEffect, useState } from 'react';

import { failureKind } from '../../../shared/errors';
import { refusalText, useRefreshOnConnect, useWhileVisible } from '../hooks';
import { fill, type Lang, type T } from '../i18n';
import { type IpcClient, api } from '../ipc';
import {
  type AuditRow,
  type FamilyDefault,
  type NodeGroup,
  type NodeInfo,
  type NodeList,
  activeGroups,
  defaultFamilies,
  nodeKind,
  nodeState,
  parseNodeList,
} from '../model';
import { Icon } from '../shell/icons';
import { StateWord } from '../ui';
import { AddNodeSheet } from './nodes/AddNodeSheet';
import { NodeSheet } from './nodes/NodeSheet';
import { groupLabel, groupWords, stateWords } from './nodes/words';

/** How often the list is refreshed while the page is visible. */
export const NODE_REFRESH_MS = 30000;
/** How often while a code for a new computer is out (the status flips to "Verbunden"). */
export const NODE_WATCH_MS = 3000;
/** How long "Laptop ist entfernt." stays. */
export const REMOVED_BANNER_MS = 8000;

export interface ComputersSectionProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  /** This dashboard runs on a paired device. */
  device: boolean;
  /** Bumped by the app on `remote.paired` / `remote.revoked`; reloads the list. */
  revision: number;
  /** The live audit rows of this session, for "Zuletzt hier getan". */
  audit: AuditRow[];
  onOpenSettings: () => void;
  onOpenHistory: () => void;
}

type View =
  | { kind: 'loading' }
  | { kind: 'unsupported' }
  | { kind: 'unreadable' }
  | { kind: 'failed'; message: string }
  | { kind: 'ready'; list: NodeList };

const TONE = { connected: 'ok', update: 'warn', offline: 'off', removed: 'off' } as const;

export function ComputersSection({
  t,
  lang,
  client,
  device,
  revision,
  audit,
  onOpenSettings,
  onOpenHistory,
}: ComputersSectionProps) {
  const [view, setView] = useState<View>({ kind: 'loading' });
  const [pinSet, setPinSet] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [watching, setWatching] = useState(false);
  const [banner, setBanner] = useState('');

  const load = useCallback(
    async (c: IpcClient, cancelled: () => boolean = () => false, background = false) => {
      try {
        const [listed, pin] = await Promise.all([
          api.nodeList(c),
          api.pinStatus(c).catch(() => ({ configured: false })),
        ]);
        if (cancelled()) return;
        const list = parseNodeList(listed);
        setPinSet((pin as { configured?: unknown }).configured === true);
        setView(list ? { kind: 'ready', list } : { kind: 'unreadable' });
      } catch (e) {
        if (cancelled()) return;
        if (failureKind(e) === 'unknown_request') {
          setView({ kind: 'unsupported' });
          return;
        }
        if (background) return;
        setView({ kind: 'failed', message: refusalText(t, e, false, '') });
      }
    },
    [t],
  );

  useRefreshOnConnect(client, load);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() sets state only after awaiting the core
    if (client && revision > 0) void load(client, () => false, true);
  }, [client, revision, load]);

  useWhileVisible(client !== null && view.kind === 'ready', adding && watching ? NODE_WATCH_MS : NODE_REFRESH_MS, () => {
    if (client) void load(client, () => false, true);
  });

  useEffect(() => {
    if (!banner) return;
    const timer = window.setTimeout(() => setBanner(''), REMOVED_BANNER_MS);
    return () => window.clearTimeout(timer);
  }, [banner]);

  const patch = (update: (list: NodeList) => NodeList) =>
    setView((v) => (v.kind === 'ready' ? { kind: 'ready', list: update(v.list) } : v));

  const onGroups = (nodeId: string, groups: NodeGroup[]) =>
    patch((list) => ({
      ...list,
      nodes: list.nodes.map((n) => (n.id === nodeId ? { ...n, groups } : n)),
    }));
  const onDefaults = (defaults: FamilyDefault[]) => patch((list) => ({ ...list, defaults }));
  const onRemoved = (node: NodeInfo) => {
    setOpenId(null);
    // The core resets every default that named the removed computer; the line says which.
    const reset = view.kind === 'ready' ? defaultFamilies(node, view.list.defaults) : [];
    setBanner(
      reset.length
        ? fill(t('node_removed_defaults'), node.name, reset.map((g) => groupLabel(t, g)).join(', '))
        : fill(t('node_removed'), node.name),
    );
    patch((list) => ({
      ...list,
      nodes: list.nodes.filter((n) => n.id !== node.id),
      defaults: list.defaults.map((d) => (d.device === node.id ? { ...d, device: 'auto', raw: 'auto' } : d)),
    }));
    if (client) void load(client, () => false, true);
  };

  if (view.kind === 'unsupported') return null;

  const heading = (
    <>
      <h2 id="h-nodes" className="sec-title">
        {t('node_section')}
      </h2>
      <p className="lede">{t('node_lede')}</p>
    </>
  );

  if (view.kind === 'loading') {
    return (
      <section aria-labelledby="h-nodes" className="sec nodes">
        {heading}
        {client && (
          <p className="hint" role="status">
            {t('lan_loading')}
          </p>
        )}
      </section>
    );
  }

  if (view.kind === 'unreadable' || view.kind === 'failed') {
    return (
      <section aria-labelledby="h-nodes" className="sec nodes">
        {heading}
        <p className="hint">
          {view.kind === 'unreadable' ? t('lan_unreadable') : view.message || t('lan_load_failed')}
        </p>
        <button
          type="button"
          className="link"
          disabled={!client}
          onClick={() => {
            if (!client) return;
            setView({ kind: 'loading' });
            void load(client);
          }}
        >
          {t('lan_retry')}
        </button>
      </section>
    );
  }

  const { list } = view;
  const nodes = list.nodes.filter((n) => !n.revoked);
  const open = nodes.find((n) => n.id === openId) ?? null;
  const canAdd = list.enabled && (!device || pinSet);

  return (
    <section aria-labelledby="h-nodes" className="sec nodes">
      {heading}
      <p role="status" className={banner ? 'banner banner--ok' : 'sr-only'}>
        {banner}
      </p>
      {!list.enabled ? (
        <>
          <p className="muted">{t('node_off')}</p>
          {!device && (
            <button type="button" className="link" onClick={onOpenSettings}>
              {t('lan_off_settings')}
            </button>
          )}
        </>
      ) : nodes.length === 0 ? (
        <>
          <p className="rail-sub">{t('node_empty')}</p>
          <p className="muted">{t('node_empty_body')}</p>
        </>
      ) : (
        <ul className="device-list node-list" aria-labelledby="h-nodes">
          {nodes.map((n) => {
            const state = nodeState(n);
            const groups = activeGroups(n);
            const families = defaultFamilies(n, list.defaults);
            return (
              <li key={n.id} className="device node">
                <span className="node-icon" aria-hidden="true">
                  <Icon name={nodeKind(n) === 'board' ? 'board' : 'computer'} />
                </span>
                <div className="device-main">
                  <p className="device-name">{n.name}</p>
                  <p className="device-meta">
                    {groups.length
                      ? fill(t('node_may'), groups.map((g) => groupWords(t, g)).join(', '))
                      : t('node_may_nothing')}
                  </p>
                  <p className="node-facts">
                    {families.length > 0 && (
                      <span className="badge">
                        {fill(t('node_default_for'), families.map((g) => groupLabel(t, g)).join(', '))}
                      </span>
                    )}
                    {n.version && <span className="device-meta">{fill(t('node_version'), n.version)}</span>}
                  </p>
                </div>
                <StateWord tone={TONE[state]} label={stateWords(t, lang, n)} />
                <div className="device-action">
                  <button
                    type="button"
                    className="btn btn--sm btn--quiet"
                    aria-label={fill(t('node_open_label'), n.name)}
                    onClick={() => setOpenId(n.id)}
                  >
                    {t('node_open')}
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {list.enabled &&
        (canAdd ? (
          <div className="btns">
            <button
              type="button"
              className={nodes.length === 0 ? 'btn btn--primary' : 'btn'}
              disabled={!client}
              onClick={() => {
                setWatching(false);
                setAdding(true);
              }}
            >
              {nodes.length === 0 ? t('node_add') : t('node_add_more')}
            </button>
          </div>
        ) : (
          <p className="hint">{t('node_only_at_computer')}</p>
        ))}

      <NodeSheet
        t={t}
        lang={lang}
        client={client}
        device={device}
        pinSet={pinSet}
        node={open}
        nodes={nodes}
        defaults={list.defaults}
        audit={audit}
        onClose={() => setOpenId(null)}
        onGroups={onGroups}
        onDefaults={onDefaults}
        onRemoved={onRemoved}
        onOpenHistory={() => {
          setOpenId(null);
          onOpenHistory();
        }}
      />
      <AddNodeSheet
        t={t}
        lang={lang}
        client={client}
        open={adding}
        pinSet={pinSet}
        nodes={nodes}
        onClose={() => {
          setAdding(false);
          setWatching(false);
        }}
        onCode={() => setWatching(true)}
      />
    </section>
  );
}
