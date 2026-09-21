/**
 * Stream page: current session status + connected plugins (from `stream.session.status` and the
 * live `obs.*`/`twitch.*` events), a live chat feed (`twitch.chat_message`), the Funken leaderboard
 * (`stream.funken.top`) and a "Privatsphäre-Szene" button that triggers the panic scene
 * (`security.panic`) — the same emergency path as the Status page's kill switch, but scoped to the
 * stream output rather than the whole assistant.
 *
 * Under the shipped default profile ("Begleiter") the OBS and Twitch plugins are not allowed to
 * load at all, so the honest thing for the plugin tiles to say is *why* they are silent and which
 * setting changes it — not "unbekannt" twice.
 */

import { useState } from 'react';

import { formatTimestamp } from '../../../shared/format';
import { useIpcAction, useRefreshOnConnect } from '../hooks';
import {
  type Lang,
  type T,
  pluginBlockedProfile,
  pluginStatusLabel,
  profileLabel,
  tierLabel,
} from '../i18n';
import { type IpcClient, api } from '../ipc';
import { type DashboardState, applyFunkenTop, applyStreamSessionStatus, viewerName } from '../model';
import { Detail, Hero, StateWord, Tile, toneFor } from '../ui';

export interface StreamPageProps {
  t: T;
  lang: Lang;
  state: DashboardState;
  /** null while offline: every request would fail, so the controls are disabled instead. */
  client: IpcClient | null;
  onState: (updater: (s: DashboardState) => DashboardState) => void;
  /** Takes the user to `security.profile` on the Settings tab. */
  onOpenSettings: () => void;
}

export function StreamPage({ t, lang, state, client, onState, onOpenSettings }: StreamPageProps) {
  const [panicSent, setPanicSent] = useState(false);
  const { busy, error, run } = useIpcAction(client, t);
  const { session, chat, funkenTop, pluginReason } = state.stream;
  const disabled = client === null;

  useRefreshOnConnect(client, async (c, cancelled) => {
    // Both requests are best-effort: the last known state is better than blanking the page, and a
    // refusal is already explained by the plugin tiles below.
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

  const refreshTop = () =>
    run('funken', async (c) => {
      const payload = await api.funkenTop(c, 10);
      onState((s) => applyFunkenTop(s, payload));
    });

  const triggerPanic = () =>
    run('panic', async (c) => {
      setPanicSent(false);
      await api.panic(c);
      setPanicSent(true);
    });

  const blockedProfile = pluginBlockedProfile(pluginReason);
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
          {session.active ? (
            <dl className="facts">
              <dt>{t('stream_session_id')}</dt>
              <dd className="break">{session.sessionId ?? unknown}</dd>
              <dt>{t('stream_started_at')}</dt>
              <dd className="break" title={session.startedAt ?? undefined}>
                {session.startedAt === null ? unknown : formatTimestamp(session.startedAt, lang)}
              </dd>
            </dl>
          ) : (
            // Nothing has started, so there is no session id and no start time. Printing
            // "unbekannt" twice would invent two unknowns for facts that simply do not exist yet.
            <p className="muted">{t('stream_idle_note')}</p>
          )}
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
          {(['obs', 'twitch'] as const).map((p) => {
            const status = session.plugins[p];
            const blocked = blockedProfile !== null && status !== 'connected';
            return (
              <Tile
                key={p}
                level={4}
                title={t(p === 'obs' ? 'stream_plugin_obs' : 'stream_plugin_twitch')}
              >
                <StateWord
                  tone={blocked ? 'warn' : toneFor(status)}
                  label={blocked ? t('plugin_profile_blocked') : pluginStatusLabel(t, status)}
                />
                {blocked ? (
                  <>
                    <p className="hint break">
                      <Detail
                        label={`${profileLabel(t, blockedProfile)} · ${t('plugin_profile_switch')}`}
                        detail={pluginReason}
                      />
                    </p>
                    <div className="tile-actions">
                      <button type="button" className="link" onClick={onOpenSettings}>
                        {t('plugin_profile_switch')}
                      </button>
                    </div>
                  </>
                ) : (
                  status !== 'connected' && (
                    <p className="hint">
                      {t(p === 'obs' ? 'plugin_obs_hint' : 'plugin_twitch_hint')}
                    </p>
                  )
                )}
              </Tile>
            );
          })}
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
                  <span className="list-meta">{viewerName(m, funkenTop) || unknown}</span>
                  <span className="break">{m.text}</span>
                </span>
                {m.addressed && (
                  <span className="list-meta nowrap">{t('stream_chat_addressed')}</span>
                )}
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
            onClick={() => void refreshTop()}
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
                    <td>{tierLabel(t, v.tier)}</td>
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
