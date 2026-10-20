/**
 * The stream, in the two places it lives now (Spec UX-Neubau §3, §6):
 *
 *  - `StreamLive` is Heute in the stream Modus: the session, the privacy scene as its own red
 *    button (a different purpose from Stopp: it secures the picture, Nox keeps running), the live
 *    chat (`twitch.chat_message`), the Funken leaderboard (`stream.funken.top`) and today's clips.
 *  - `StreamTech` is Geräte › Stream-Technik: whether OBS and Twitch are connected, and - when the
 *    security profile keeps them from loading - why, with the one fix: the stream Modus, which
 *    switches mode and profile together.
 *
 * Setting the stream up (overlay, own audio track, logins) lives in Einstellungen › Stream.
 */

import { useState } from 'react';

import { formatTimestamp } from '../../../shared/format';
import { useIpcAction } from '../hooks';
import { type Lang, type T, pluginBlockedProfile, pluginStatusLabel, profileLabel, tierLabel } from '../i18n';
import { type IpcClient, api } from '../ipc';
import { type DashboardState, applyFunkenTop, viewerName } from '../model';
import { Detail, StateWord, toneFor } from '../ui';

export interface StreamProps {
  t: T;
  lang: Lang;
  state: DashboardState;
  /** null while offline: every request would fail, so the controls are disabled instead. */
  client: IpcClient | null;
  onState: (updater: (s: DashboardState) => DashboardState) => void;
}

export function StreamLive({ t, lang, state, client, onState }: StreamProps) {
  const [panicSent, setPanicSent] = useState(false);
  const { busy, error, run } = useIpcAction(client, t);
  const { session, chat, funkenTop } = state.stream;
  const disabled = client === null;
  const unknown = t('unknown');

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

  const today = new Date().toDateString();
  const clipsToday = state.clips.items.filter((c) => new Date(c.createdAt).toDateString() === today);

  return (
    <div className="live">
      <div className="live-h">
        {/* Red only while really live (Spec §2: red means Stop, Live and errors). */}
        <span className={session.active ? 'badge badge--danger' : 'badge'}>
          {session.active && <span className="lamp lamp--sm" data-s="live" aria-hidden="true" />}
          {session.active ? t('stream_active') : t('stream_inactive')}
        </span>
        <h2 className="sec-title">{session.scene ?? t('stream_session_title')}</h2>
        <button
          type="button"
          className="btn btn--danger"
          disabled={disabled || busy === 'panic'}
          aria-describedby="privacy-scene-hint"
          onClick={() => void triggerPanic()}
        >
          {t('stream_privacy_button')}
        </button>
      </div>
      <p id="privacy-scene-hint" className="hint">
        {t('stream_privacy_hint')}
      </p>
      {panicSent && (
        <p role="status" className="ok-note">
          {t('stream_privacy_sent')}
        </p>
      )}
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

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
        // Nothing has started, so there is no session id and no start time. Printing "unbekannt"
        // twice would invent two unknowns for facts that simply do not exist yet.
        <p className="muted">{t('stream_idle_note')}</p>
      )}

      <section className="sec" aria-labelledby="h-stream-chat">
        <div className="sec-h">
          <h2 id="h-stream-chat" className="sec-title">
            {t('stream_chat_title')}
          </h2>
        </div>
        <p className="lede">{t('stream_chat_hint')}</p>
        {chat.length === 0 ? (
          <p className="muted">{t('stream_chat_empty')}</p>
        ) : (
          <ul className="list list--rows list--scroll chatfeed">
            {chat.map((m) => (
              <li key={m.id} className="row">
                <span className="who">{viewerName(m, funkenTop) || unknown}</span>
                <span className="grow break">{m.text}</span>
                {m.addressed && <span className="badge badge--attn">{t('stream_chat_addressed')}</span>}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="sec" aria-labelledby="h-funken">
        <div className="sec-h">
          <h2 id="h-funken" className="sec-title">
            {t('stream_funken_title')}
          </h2>
          <button
            type="button"
            className="btn btn--sm btn--plain"
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

      <section className="sec" aria-labelledby="h-clips-today">
        <div className="sec-h">
          <h2 id="h-clips-today" className="sec-title">
            {t('ux_clips_today')}
          </h2>
        </div>
        {clipsToday.length === 0 ? (
          <p className="muted">{t('ux_clips_today_empty')}</p>
        ) : (
          <ul className="list list--rows">
            {clipsToday.map((clip) => (
              <li key={clip.id} className="row">
                <span className="grow">
                  <span className="row-t">{clip.tags.length > 0 ? clip.tags.join(', ') : clip.id}</span>
                  <span className="row-d">{formatTimestamp(clip.createdAt, lang)}</span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

export interface StreamTechProps extends StreamProps {
  device: boolean;
  onOpenStreamSettings: () => void;
}

export function StreamTech({ t, state, client, device, onOpenStreamSettings }: StreamTechProps) {
  const { busy, error, run } = useIpcAction(client, t);
  const { session, pluginReason } = state.stream;
  const blockedProfile = pluginBlockedProfile(pluginReason);

  return (
    <>
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}
      <ul className="list list--rows">
        {(['obs', 'twitch'] as const).map((p) => {
          const status = session.plugins[p];
          const blocked = blockedProfile !== null && status !== 'connected';
          return (
            <li key={p} className="row">
              <span className="grow">
                <span className="row-t">{t(p === 'obs' ? 'stream_plugin_obs' : 'stream_plugin_twitch')}</span>
                {blocked ? (
                  <span className="row-d break">
                    <Detail
                      label={`${profileLabel(t, blockedProfile)} · ${t('ux_stream_blocked')}`}
                      detail={pluginReason}
                    />
                  </span>
                ) : (
                  status !== 'connected' && (
                    <span className="row-d">{t(p === 'obs' ? 'plugin_obs_hint' : 'plugin_twitch_hint')}</span>
                  )
                )}
              </span>
              <StateWord
                tone={blocked ? 'warn' : toneFor(status)}
                label={blocked ? t('plugin_profile_blocked') : pluginStatusLabel(t, status)}
              />
            </li>
          );
        })}
      </ul>
      <div className="btns">
        {blockedProfile !== null && !device && (
          <button
            type="button"
            className="btn btn--sm"
            disabled={client === null || busy === 'mode'}
            onClick={() => void run('mode', (c) => api.setMode(c, 'stream'))}
          >
            {t('ux_stream_to_mode')}
          </button>
        )}
        <button type="button" className="btn btn--sm btn--plain" onClick={onOpenStreamSettings}>
          {t('ux_stream_setup_link')}
        </button>
      </div>
    </>
  );
}
