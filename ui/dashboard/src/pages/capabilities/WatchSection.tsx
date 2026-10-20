/**
 * "Wächter": what Nox checks regularly because somebody said "sag mir, wenn ...".
 *
 * Watchers are created in chat or by voice, never here — the page says so when there are none.
 * Each row reads as a sentence ("prüft web.fetch alle 15 min: enthält „Min“ nach „RB 40“"),
 * followed by what the last check saw, the last error, and when it last reported.
 */

import { useCallback, useState } from 'react';

import { formatTimestamp, timestampTitle } from '../../../../shared/format';
import type { Lang } from '../../../../shared/lang';
import { useIpcAction } from '../../hooks';
import { type Key, type T, fill } from '../../i18n';
import { type IpcClient, api } from '../../ipc';
import { type WatchCondition, type Watcher, parseActionResult, parseWatchers } from '../../model';
import { Tile } from '../../ui';
import { loadError, useReload } from './shared';

const OP_KEYS: Record<string, Key> = {
  lt: 'caps_watch_op_lt',
  gt: 'caps_watch_op_gt',
  contains: 'caps_watch_op_contains',
  changed: 'caps_watch_op_changed',
};

/** "enthält „Min“ nach „RB 40“" — the condition as words; an unknown operator stays visible. */
function conditionText(t: T, condition: WatchCondition): string {
  const key = OP_KEYS[condition.op];
  const value = condition.value ?? '';
  const base = key ? fill(t(key), value) : `${condition.op} ${value}`.trim();
  return condition.near ? fill(t('caps_watch_near'), base, condition.near) : base;
}

/** "prüft web.fetch alle 15 min: ..." */
function watcherSentence(t: T, watcher: Watcher): string {
  const interval = watcher.intervalMin === null ? '?' : watcher.intervalMin;
  return fill(t('caps_watch_rule'), watcher.tool, interval, conditionText(t, watcher.condition));
}

export interface WatchSectionProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  revision: number;
}

export function WatchSection({ t, lang, client, revision }: WatchSectionProps) {
  const [watchers, setWatchers] = useState<Watcher[] | null>(null);
  const [loadFailed, setLoadFailed] = useState('');
  const [refusal, setRefusal] = useState('');
  const action = useIpcAction(client, t);

  const load = useCallback(
    async (c: IpcClient, cancelled: () => boolean = () => false) => {
      try {
        const parsed = parseWatchers(await api.watchList(c));
        if (cancelled()) return;
        setWatchers(parsed);
        setLoadFailed('');
      } catch (e) {
        if (!cancelled()) setLoadFailed(loadError(t, e));
      }
    },
    [t],
  );
  useReload(client, revision, load);

  const remove = (id: string) => {
    setRefusal('');
    void action.run(`delete:${id}`, async (c) => {
      const result = parseActionResult(await api.watchDelete(c, id));
      if (!result.ok) setRefusal(fill(t('caps_watch_delete_failed'), result.reason || t('unknown')));
      await load(c);
    });
  };

  const time = (iso: string | null) => (iso ? formatTimestamp(iso, lang) : t('caps_watch_never'));

  return (
    <section className="caps-section" aria-labelledby="h-caps-watch">
      <div className="rail-head">
        <div>
          <h2 id="h-caps-watch" className="sec-title">
            {t('caps_watch_title')}
          </h2>
          <p className="rail-sub">{t('caps_watch_lede')}</p>
        </div>
      </div>

      {loadFailed && (
        <p role="alert" className="alert">
          {loadFailed}
        </p>
      )}
      {(refusal || action.error) && (
        <p role="alert" className="alert">
          {refusal || action.error}
        </p>
      )}

      <div className="tiles tiles--single">
        {watchers && watchers.length === 0 && (
          <Tile level={4} title={t('caps_watch_empty')}>
            <p className="muted">{t('caps_watch_empty_hint')}</p>
          </Tile>
        )}
        {watchers?.map((watcher) => (
          <Tile key={watcher.id} level={4} id={`watch-${watcher.id}`} title={watcher.title || watcher.id}>
            <p>{watcherSentence(t, watcher)}</p>
            {watcher.condition.path && (
              <p className="hint">
                {t('caps_watch_path')} <code className="mono">{watcher.condition.path}</code>
              </p>
            )}
            <dl className="facts">
              <dt>{t('caps_watch_last_checked')}</dt>
              <dd title={timestampTitle(watcher.lastCheckedAt)}>{time(watcher.lastCheckedAt)}</dd>
              <dt>{t('caps_watch_last_value')}</dt>
              <dd className="break">{watcher.lastValue ?? '–'}</dd>
              <dt>{t('caps_watch_last_fired')}</dt>
              <dd title={timestampTitle(watcher.lastFiredAt)}>{time(watcher.lastFiredAt)}</dd>
              <dt>{t('caps_watch_counts_label')}</dt>
              <dd>{fill(t('caps_watch_counts'), watcher.checks, watcher.fired)}</dd>
            </dl>
            {watcher.lastError && (
              <p className="alert">{fill(t('caps_watch_error'), watcher.lastError)}</p>
            )}
            <div className="tile-actions">
              <button
                type="button"
                className="btn btn--quiet btn--sm"
                disabled={client === null || action.busy !== null}
                aria-describedby={`watch-${watcher.id}-title`}
                onClick={() => remove(watcher.id)}
              >
                {t('caps_watch_delete')}
              </button>
            </div>
          </Tile>
        ))}
      </div>
    </section>
  );
}
