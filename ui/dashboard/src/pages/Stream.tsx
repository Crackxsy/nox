/**
 * Stream page: current session status + connected plugins (from `stream.session.status` and the
 * live `obs.*`/`twitch.*` events), a live chat feed (`twitch.chat_message`), the Funken leaderboard
 * (`stream.funken.top`) and a "Privacy scene" button that triggers the panic scene
 * (`security.panic`) — the same emergency path as the Status page's kill switch, but scoped to the
 * stream output rather than the whole assistant.
 */

import { useEffect, useState } from 'react';

import { type T, pluginStatusLabel } from '../i18n';
import { type IpcClient, api } from '../ipc';
import { type DashboardState, applyFunkenTop, applyStreamSessionStatus } from '../model';
import { Hero, StateWord, Tile } from '../ui';

export interface StreamPageProps {
  t: T;
  state: DashboardState;
  /** null while offline: every request would fail, so the controls are disabled instead. */
  client: IpcClient | null;
  onState: (updater: (s: DashboardState) => DashboardState) => void;
}

export function StreamPage({ t, state, client, onState }: StreamPageProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [panicSent, setPanicSent] = useState(false);
  const { session, chat, funkenTop } = state.stream;
  const disabled = client === null;

  const refreshTop = async (c: IpcClient) => {
    setBusy('funken');
    setError('');
    try {
      const payload = await api.funkenTop(c, 10);
      onState((s) => applyFunkenTop(s, payload));
    } catch (e) {
      setError(`${t('error_prefix')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  useEffect(() => {
    if (!client) return;
    let cancelled = false;
    (async () => {
      try {
        const status = await api.streamStatus(client);
        if (!cancelled) onState((s) => applyStreamSessionStatus(s, status));
      } catch {
        // handled the same way the Status page treats a failed refresh: keep the last known state.
      }
      try {
        const top = await api.funkenTop(client, 10);
        if (!cancelled) onState((s) => applyFunkenTop(s, top));
      } catch {
        // ditto
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run once per (re)connect
  }, [client]);

  const triggerPanic = async () => {
    if (!client) return;
    setBusy('panic');
    setError('');
    setPanicSent(false);
    try {
      await api.panic(client);
      setPanicSent(true);
    } catch (e) {
      setError(`${t('error_prefix')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const unknown = t('unknown');

  return (
    <div className="page wrap">
      <Hero
        title={t('hero_stream')}
        sub={t('hero_stream_sub')}
        links={
          <a className="link" href="#privacy-scene">
            {t('jump_privacy')}
          </a>
        }
      />

      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      <div className="tiles tiles--single">
        <Tile
          feature
          id="session"
          eyebrow={session.active ? t('stream_active') : t('stream_inactive')}
          title={session.scene ?? t('stream_session_title')}
        >
          <dl className="facts">
            <dt>{t('stream_session_id')}</dt>
            <dd className="break">{session.sessionId ?? unknown}</dd>
            <dt>{t('stream_started_at')}</dt>
            <dd className="break">{session.startedAt ?? unknown}</dd>
          </dl>
        </Tile>
      </div>

      <section aria-labelledby="h-plugins">
        <div className="rail-head">
          <div>
            <h3 id="h-plugins" className="rail-title">
              {t('stream_plugins_title')}
            </h3>
          </div>
        </div>
        <div className="tiles">
          {(['obs', 'twitch'] as const).map((p) => (
            <Tile
              key={p}
              title={t(p === 'obs' ? 'stream_plugin_obs' : 'stream_plugin_twitch')}
            >
              <StateWord
                status={session.plugins[p]}
                label={pluginStatusLabel(t, session.plugins[p])}
              />
            </Tile>
          ))}
        </div>
      </section>

      <section aria-labelledby="h-stream-chat">
        <div className="rail-head">
          <div>
            <h3 id="h-stream-chat" className="rail-title">
              {t('stream_chat_title')}
            </h3>
            <p className="rail-sub">{t('stream_chat_hint')}</p>
          </div>
        </div>
        {chat.length === 0 ? (
          <p className="muted">{t('stream_chat_empty')}</p>
        ) : (
          <ul className="list list--scroll">
            {chat.map((m) => (
              <li key={m.id}>
                <span className="list-main">
                  <span className="list-meta">{m.viewerId || unknown}</span>
                  <span className="break">{m.text}</span>
                </span>
                <span className="list-meta nowrap">
                  {m.addressed ? t('stream_chat_addressed') : m.relevance.toFixed(2)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="h-funken">
        <div className="rail-head">
          <div>
            <h3 id="h-funken" className="rail-title">
              {t('stream_funken_title')}
            </h3>
          </div>
          <button
            type="button"
            className="btn btn--quiet btn--sm"
            disabled={disabled || busy === 'funken'}
            onClick={() => client && void refreshTop(client)}
          >
            {t('stream_funken_refresh')}
          </button>
        </div>
        {funkenTop.length === 0 ? (
          <p className="muted">{t('stream_funken_empty')}</p>
        ) : (
          <div className="table-scroll">
            <table className="specs">
              <caption className="sr-only">{t('stream_funken_title')}</caption>
              <thead>
                <tr>
                  <th scope="col">{t('funken_rank')}</th>
                  <th scope="col">{t('funken_viewer')}</th>
                  <th scope="col">{t('funken_balance')}</th>
                  <th scope="col">{t('funken_tier')}</th>
                </tr>
              </thead>
              <tbody>
                {funkenTop.map((v, i) => (
                  <tr key={v.viewerId}>
                    <th scope="row" className="num">
                      {i + 1}
                    </th>
                    <td className="break">{v.displayName || v.viewerId}</td>
                    <td className="num">{v.balance}</td>
                    <td>{v.tier}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="tiles tiles--single">
        <Tile
          id="privacy-scene"
          eyebrow={t('privacy_title')}
          title={t('stream_privacy_title')}
          lede={t('stream_privacy_hint')}
        >
          <div className="tile-actions">
            <button
              type="button"
              className="btn btn--danger"
              disabled={disabled || busy === 'panic'}
              onClick={() => void triggerPanic()}
            >
              {t('stream_privacy_button')}
            </button>
            {panicSent && (
              <p role="status" className="ok-note">
                {t('stream_privacy_sent')}
              </p>
            )}
          </div>
        </Tile>
      </div>
    </div>
  );
}
