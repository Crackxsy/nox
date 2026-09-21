/**
 * Status page: one glowing headline, a feature tile with the live snapshot, the health report as an
 * Apple tech-specs list, the AI providers as a snap rail, then the controls (privacy mode,
 * assistant mode, mute) and the kill switch.
 *
 * Every control disables itself when the core is unreachable, and nothing is optimistically
 * flipped: the UI only changes when the core answers or an event arrives.
 *
 * The health report stays a real <table> (component row header, status cell, reason cell) — it is a
 * data grid and the e2e smoke test reads it as one. The component cell shows the readable name with
 * the core's id underneath, so the screen is legible *and* the id stays greppable.
 */

import { useEffect, useRef, useState } from 'react';

import { formatTimestamp } from '../../../shared/format';
import { useIpcAction } from '../hooks';
import {
  type Key,
  type Lang,
  type T,
  componentLabel,
  levelLabel,
  modeHeadline,
  modeLabel,
  privacyLabel,
  reasonLine,
  roleList,
  routingLabel,
  statusLabel,
} from '../i18n';
import { type IpcClient, api } from '../ipc';
import {
  type CaptureFlags,
  type DashboardState,
  KILL_CONFIRM_MS,
  type KillPhase,
  MODES,
  PRIVACY_MODES,
  killNext,
} from '../model';
import { Detail, Hero, Rail, StateWord, Tile, toneFor } from '../ui';

export interface StatusPageProps {
  t: T;
  lang: Lang;
  state: DashboardState;
  /** null while offline: every request would fail, so the controls are disabled instead. */
  client: IpcClient | null;
  /** True once `ai.providers` failed even after the retries in `App.tsx`. */
  providersFailed: boolean;
  onRefresh: () => void;
}

/**
 * The armed step expires. That is a promise the UI has to keep *visibly*, so the confirm row
 * carries a ring that drains over exactly `KILL_CONFIRM_MS` — the CSS animation and the component's
 * timer are the same number, read from the same token (`--dur-arm`). Decorative, never interactive;
 * the countdown itself is already announced by the `role="alert"` hint below the buttons.
 */
const RING_R = 13;
const RING_C = 2 * Math.PI * RING_R;

function KillCountdown() {
  return (
    <svg
      className="kill-ring"
      width="30"
      height="30"
      viewBox="0 0 30 30"
      aria-hidden="true"
      focusable="false"
      style={{ ['--kill-ring-circumference' as string]: RING_C.toFixed(2) }}
    >
      <circle className="kill-ring-track" cx="15" cy="15" r={RING_R} strokeWidth="3" />
      <circle className="kill-ring-sweep" cx="15" cy="15" r={RING_R} strokeWidth="3" />
    </svg>
  );
}

const CAPTURE_KEYS = ['microphone', 'camera', 'screen', 'cloud'] as const;
const CAPTURE_LABEL: Record<keyof CaptureFlags, Key> = {
  microphone: 'capture_microphone',
  camera: 'capture_camera',
  screen: 'capture_screen',
  cloud: 'capture_cloud',
};

export function StatusPage({ t, lang, state, client, providersFailed, onRefresh }: StatusPageProps) {
  const { busy, error, run } = useIpcAction(client, t);
  /** Only an explicit choice lives here; `null` means "show whatever the core last reported". */
  const [privacyChoice, setPrivacyChoice] = useState<string | null>(null);
  const [modeChoice, setModeChoice] = useState<string | null>(null);
  const [killPhase, setKillPhase] = useState<KillPhase>('idle');
  const [killReason, setKillReason] = useState('');
  const killTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (killTimer.current) clearTimeout(killTimer.current);
    },
    [],
  );

  /**
   * Two presses, and only two. `killNext` makes `sent` terminal, so the second branch below cannot
   * fire again after a kill; the button then renders as a disabled "ausgelöst" state, because
   * resuming from safe mode is deliberately not a dashboard action.
   */
  const armKill = () => {
    const next = killNext(killPhase, 'press');
    if (killPhase === 'idle' && next === 'confirm') {
      setKillPhase('confirm');
      killTimer.current = setTimeout(() => setKillPhase('idle'), KILL_CONFIRM_MS);
      return;
    }
    if (killPhase === 'confirm' && next === 'sent') {
      if (killTimer.current) clearTimeout(killTimer.current);
      setKillPhase('sent');
      void run('kill', (c) => api.kill(c, killReason || 'dashboard kill switch'));
    }
  };

  const cancelKill = () => {
    if (killTimer.current) clearTimeout(killTimer.current);
    if (killPhase !== 'sent') setKillPhase('idle');
  };

  const disabled = client === null;
  const muted = state.muted;
  const safeMode = state.systemLevel === 'safe_mode';
  const killed = killPhase === 'sent' || safeMode;
  const unknown = t('unknown');
  const privacyValue = privacyChoice ?? state.privacyMode ?? '';
  const modeValue = modeChoice ?? state.mode ?? '';

  return (
    <div className="page wrap">
      <Hero
        glow
        title={t('hero_status')}
        sub={t('hero_status_sub')}
        links={
          <>
            <button type="button" className="link" onClick={onRefresh} disabled={disabled}>
              {t('status_refresh')}
            </button>
            <a className="link" href="#kill-switch">
              {t('jump_kill')}
            </a>
          </>
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
          id="now"
          eyebrow={state.systemLevel ? levelLabel(t, state.systemLevel) : unknown}
          title={state.mode ? modeHeadline(t, state.mode) : unknown}
        >
          <dl className="facts">
            <dt>{t('privacy_title')}</dt>
            <dd>{state.privacyMode ? privacyLabel(t, state.privacyMode) : unknown}</dd>
            <dt>{t('mute_title')}</dt>
            <dd>{muted === null ? unknown : muted ? t('muted_yes') : t('muted_no')}</dd>
            <dt>{t('voice_listening')}</dt>
            <dd>
              {state.voice ? (state.voice.listening ? t('capture_on') : t('capture_off')) : unknown}
            </dd>
            <dt>{t('voice_speaking')}</dt>
            <dd>
              {state.voice ? (state.voice.speaking ? t('capture_on') : t('capture_off')) : unknown}
            </dd>
          </dl>

          <h4 className="subhead">{t('capture_title')}</h4>
          {state.capture ? (
            <ul className="list">
              {CAPTURE_KEYS.map((k) => {
                const on = state.capture?.[k] === true;
                return (
                  <li key={k}>
                    <span>{t(CAPTURE_LABEL[k])}</span>
                    <StateWord
                      tone={on ? 'warn' : 'off'}
                      label={on ? t('capture_on') : t('capture_off')}
                    />
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="muted">{unknown}</p>
          )}
        </Tile>
      </div>

      <section aria-labelledby="h-health">
        <div className="rail-head">
          <div>
            <h3 id="h-health" className="rail-title">
              {t('health_title')}
            </h3>
            {state.health?.generatedAt && (
              <p className="rail-sub">
                {t('health_generated')}:{' '}
                <span title={state.health.generatedAt}>
                  {formatTimestamp(state.health.generatedAt, lang)}
                </span>
              </p>
            )}
          </div>
        </div>
        {state.health && state.health.components.length > 0 ? (
          <div className="table-scroll">
            <table className="specs">
              <caption className="sr-only">{t('health_title')}</caption>
              <thead>
                <tr>
                  <th scope="col">{t('health_component')}</th>
                  <th scope="col">{t('health_status')}</th>
                  <th scope="col">{t('health_reason')}</th>
                </tr>
              </thead>
              <tbody>
                {state.health.components.map((c) => {
                  const reason = reasonLine(t, lang, c.reason);
                  return (
                    <tr key={c.component}>
                      <th scope="row">
                        <Detail label={componentLabel(t, c.component)} detail={c.component} />
                      </th>
                      <td>
                        <StateWord tone={toneFor(c.status)} label={statusLabel(t, c.status)} />
                      </td>
                      <td className="muted break">
                        {reason.text ? <Detail label={reason.text} detail={reason.original} /> : '–'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted">{t('health_empty')}</p>
        )}
      </section>

      {state.providers && state.providers.length > 0 ? (
        <Rail t={t} title={t('providers_title')}>
          {state.providers.map((p) => {
            const reason = reasonLine(t, lang, p.reason);
            return (
              <Tile
                key={p.id}
                level={4}
                eyebrow={p.local ? t('provider_local') : t('provider_cloud')}
                title={p.displayName}
                lede={p.roles.length > 0 ? roleList(t, p.roles) : undefined}
              >
                <StateWord tone={toneFor(p.status)} label={statusLabel(t, p.status)} />
                {reason.text && (
                  <p className="hint break">
                    <Detail label={reason.text} detail={reason.original} />
                  </p>
                )}
              </Tile>
            );
          })}
        </Rail>
      ) : (
        <section aria-labelledby="h-providers">
          <div className="rail-head">
            <div>
              <h3 id="h-providers" className="rail-title">
                {t('providers_title')}
              </h3>
              <p className="rail-sub">
                {providersFailed ? t('providers_failed') : t('providers_empty')}
              </p>
            </div>
          </div>
          <button type="button" className="link" onClick={onRefresh} disabled={disabled}>
            {t('providers_retry')}
          </button>
        </section>
      )}

      <div className="tiles">
        <Tile id="privacy" title={t('privacy_title')}>
          <div className="field">
            <label htmlFor="privacy-mode" className="label">
              {t('privacy_mode')}
            </label>
            <select
              id="privacy-mode"
              className="select"
              value={privacyValue}
              disabled={disabled}
              onChange={(e) => setPrivacyChoice(e.target.value)}
            >
              {state.privacyMode === null && <option value="">{unknown}</option>}
              {PRIVACY_MODES.map((m) => (
                <option key={m} value={m}>
                  {privacyLabel(t, m)}
                </option>
              ))}
            </select>
          </div>
          <div className="tile-actions">
            <button
              type="button"
              className="btn"
              disabled={
                disabled || busy === 'privacy' || !privacyValue || privacyValue === state.privacyMode
              }
              onClick={() => {
                void run('privacy', (c) => api.setPrivacy(c, privacyValue)).then(() =>
                  setPrivacyChoice(null),
                );
              }}
            >
              {t('privacy_apply')}
            </button>
          </div>
        </Tile>

        <Tile id="mode" title={t('mode_title')}>
          <div className="field">
            <label htmlFor="assistant-mode" className="label">
              {t('mode_title')}
            </label>
            <select
              id="assistant-mode"
              className="select"
              value={modeValue}
              disabled={disabled}
              onChange={(e) => setModeChoice(e.target.value)}
            >
              {state.mode === null && <option value="">{unknown}</option>}
              {MODES.map((m) => (
                <option key={m} value={m}>
                  {modeLabel(t, m)}
                </option>
              ))}
            </select>
          </div>
          <div className="tile-actions">
            <button
              type="button"
              className="btn"
              disabled={disabled || busy === 'mode' || !modeValue || modeValue === state.mode}
              onClick={() => {
                void run('mode', (c) => api.setMode(c, modeValue)).then(() => setModeChoice(null));
              }}
            >
              {t('mode_apply')}
            </button>
          </div>
        </Tile>

        <Tile id="voice" title={t('voice_title')}>
          {state.voice ? (
            <dl className="facts">
              <dt>{t('voice_stt')}</dt>
              <dd>{state.voice.sttEngine || unknown}</dd>
              <dt>{t('voice_tts')}</dt>
              <dd>{state.voice.ttsEngine || unknown}</dd>
              <dt>{t('voice_routing')}</dt>
              <dd>{state.voice.routing ? routingLabel(t, state.voice.routing) : unknown}</dd>
              <dt>{t('voice_latency')}</dt>
              <dd>
                {state.voice.lastLatencyMs === null ? unknown : `${state.voice.lastLatencyMs} ms`}
              </dd>
            </dl>
          ) : (
            <p className="muted">{unknown}</p>
          )}
          <div className="tile-actions">
            <button
              type="button"
              className="btn btn--quiet"
              disabled={disabled || busy === 'mute' || muted === null}
              aria-pressed={muted === true}
              onClick={() => void run('mute', (c) => api.setMuted(c, !muted))}
            >
              {muted ? t('mute_off') : t('mute_on')}
            </button>
          </div>
        </Tile>
      </div>

      <div className="tiles tiles--single">
        <Tile
          feature
          id="kill-switch"
          eyebrow={safeMode ? t('kill_engaged') : undefined}
          title={t('kill_title')}
          lede={t('kill_explain')}
        >
          <div className="field">
            <label htmlFor="kill-reason" className="label">
              {t('kill_reason_label')}
            </label>
            <input
              id="kill-reason"
              className="input"
              value={killReason}
              disabled={disabled || killed}
              onChange={(e) => setKillReason(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') cancelKill();
              }}
            />
          </div>
          <div className="tile-actions">
            <button
              type="button"
              className={killPhase === 'confirm' ? 'btn btn--armed' : 'btn btn--danger'}
              disabled={disabled || busy === 'kill' || killed}
              aria-describedby="kill-hint"
              onClick={armKill}
              onKeyDown={(e) => {
                if (e.key === 'Escape') cancelKill();
              }}
            >
              {killPhase === 'confirm' ? t('kill_confirm_button') : t('kill_button')}
            </button>
            {killPhase === 'confirm' && (
              <>
                <KillCountdown />
                <button type="button" className="btn btn--quiet" onClick={cancelKill}>
                  {t('kill_cancel')}
                </button>
              </>
            )}
          </div>
          <p
            id="kill-hint"
            role={killPhase === 'idle' ? undefined : 'alert'}
            className={killPhase === 'sent' ? 'ok-note' : 'hint'}
          >
            {killPhase === 'confirm'
              ? t('kill_confirm')
              : killPhase === 'sent'
                ? t('kill_sent')
                : ''}
          </p>
        </Tile>
      </div>
    </div>
  );
}
