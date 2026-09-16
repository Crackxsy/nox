/**
 * Status page: one glowing headline, a black "right now" tile with the live snapshot, the health
 * report as an Apple tech-specs list, the AI providers as a snap rail, then the controls (privacy
 * mode, assistant mode, mute) and the kill switch.
 *
 * Every control disables itself when the core is unreachable, and nothing is optimistically
 * flipped: the UI only changes when the core answers or an event arrives.
 *
 * The health report stays a real <table> (component row header, status cell, reason cell) — it is a
 * data grid and the e2e smoke test reads it as one; only the skin is new.
 */

import { useEffect, useRef, useState } from 'react';

import { type Key, type T, statusLabel } from '../i18n';
import { type IpcClient, api } from '../ipc';
import {
  type DashboardState,
  type KillPhase,
  KILL_CONFIRM_MS,
  MODES,
  PRIVACY_MODES,
  killNext,
} from '../model';
import { Hero, Rail, StateWord, Tile } from '../ui';

export interface StatusPageProps {
  t: T;
  state: DashboardState;
  /** null while offline: every request would fail, so the controls are disabled instead. */
  client: IpcClient | null;
  /** True once `ai.providers` failed even after the retries in `App.tsx`. */
  providersFailed: boolean;
  onRefresh: () => void;
}

export function StatusPage({ t, state, client, providersFailed, onRefresh }: StatusPageProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string>('');
  const [privacyChoice, setPrivacyChoice] = useState<string>('');
  const [modeChoice, setModeChoice] = useState<string>('');
  const [killPhase, setKillPhase] = useState<KillPhase>('idle');
  const [killReason, setKillReason] = useState('');
  const killTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => setPrivacyChoice(state.privacyMode ?? ''), [state.privacyMode]);
  useEffect(() => setModeChoice(state.mode ?? ''), [state.mode]);
  useEffect(
    () => () => {
      if (killTimer.current) clearTimeout(killTimer.current);
    },
    [],
  );

  const run = async (label: string, fn: (c: IpcClient) => Promise<unknown>) => {
    if (!client) return;
    setBusy(label);
    setError('');
    try {
      await fn(client);
    } catch (e) {
      setError(`${t('error_prefix')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const armKill = () => {
    const next = killNext(killPhase, 'press');
    if (next === 'confirm') {
      setKillPhase('confirm');
      killTimer.current = setTimeout(() => setKillPhase('idle'), KILL_CONFIRM_MS);
      return;
    }
    if (next === 'sent') {
      if (killTimer.current) clearTimeout(killTimer.current);
      setKillPhase('sent');
      void run('kill', (c) => api.kill(c, killReason || 'dashboard kill switch'));
    }
  };

  const cancelKill = () => {
    if (killTimer.current) clearTimeout(killTimer.current);
    setKillPhase('idle');
  };

  const disabled = client === null;
  const muted = state.muted;
  const safeMode = state.systemLevel === 'safe_mode';
  const unknown = t('unknown');

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
          eyebrow={state.systemLevel ?? unknown}
          title={state.mode ?? unknown}
        >
          <dl className="facts">
            <dt>{t('privacy_title')}</dt>
            <dd>{state.privacyMode ?? unknown}</dd>
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
              {(['microphone', 'camera', 'screen', 'cloud'] as const).map((k) => (
                <li key={k}>
                  <span>{t(`capture_${k}` as Key)}</span>
                  <StateWord
                    status={state.capture?.[k] ? 'limited' : 'off'}
                    label={state.capture?.[k] ? t('capture_on') : t('capture_off')}
                  />
                </li>
              ))}
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
                {t('health_generated')}: {state.health.generatedAt}
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
                {state.health.components.map((c) => (
                  <tr key={c.component}>
                    <th scope="row">{c.component}</th>
                    <td>
                      <StateWord status={c.status} label={statusLabel(t, c.status)} />
                    </td>
                    <td className="muted break">{c.reason || '–'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted">{t('health_empty')}</p>
        )}
      </section>

      {state.providers && state.providers.length > 0 ? (
        <Rail t={t} title={t('providers_title')}>
          {state.providers.map((p) => (
            <Tile
              key={p.id}
              eyebrow={p.local ? t('provider_local') : t('provider_cloud')}
              title={p.displayName}
              lede={p.roles.length > 0 ? p.roles.join(', ') : undefined}
            >
              <StateWord status={p.status} label={statusLabel(t, p.status)} />
              {p.reason && <p className="hint break">{p.reason}</p>}
            </Tile>
          ))}
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
        <Tile id="privacy" eyebrow={state.privacyMode ?? unknown} title={t('privacy_title')}>
          <div className="field">
            <label htmlFor="privacy-mode" className="label">
              {t('privacy_mode')}
            </label>
            <select
              id="privacy-mode"
              className="select"
              value={privacyChoice}
              disabled={disabled}
              onChange={(e) => setPrivacyChoice(e.target.value)}
            >
              {state.privacyMode === null && <option value="">{unknown}</option>}
              {PRIVACY_MODES.map((m) => (
                <option key={m} value={m}>
                  {t(`privacy_${m}` as Key)}
                </option>
              ))}
            </select>
          </div>
          <div className="tile-actions">
            <button
              type="button"
              className="btn"
              disabled={
                disabled ||
                busy === 'privacy' ||
                !privacyChoice ||
                privacyChoice === state.privacyMode
              }
              onClick={() => void run('privacy', (c) => api.setPrivacy(c, privacyChoice))}
            >
              {t('privacy_apply')}
            </button>
          </div>
        </Tile>

        <Tile id="mode" eyebrow={state.mode ?? unknown} title={t('mode_title')}>
          <div className="field">
            <label htmlFor="assistant-mode" className="label">
              {t('mode_title')}
            </label>
            <select
              id="assistant-mode"
              className="select"
              value={modeChoice}
              disabled={disabled}
              onChange={(e) => setModeChoice(e.target.value)}
            >
              {state.mode === null && <option value="">{unknown}</option>}
              {MODES.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>
          <div className="tile-actions">
            <button
              type="button"
              className="btn"
              disabled={disabled || busy === 'mode' || !modeChoice || modeChoice === state.mode}
              onClick={() => void run('mode', (c) => api.setMode(c, modeChoice))}
            >
              {t('mode_apply')}
            </button>
          </div>
        </Tile>

        <Tile
          id="voice"
          eyebrow={muted === null ? unknown : muted ? t('muted_yes') : t('muted_no')}
          title={t('voice_title')}
        >
          {state.voice ? (
            <dl className="facts">
              <dt>{t('voice_stt')}</dt>
              <dd>{state.voice.sttEngine || unknown}</dd>
              <dt>{t('voice_tts')}</dt>
              <dd>{state.voice.ttsEngine || unknown}</dd>
              <dt>{t('voice_routing')}</dt>
              <dd>{state.voice.routing || unknown}</dd>
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
          eyebrow={safeMode ? t('kill_engaged') : t('kill_title')}
          title={t('kill_button')}
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
              disabled={disabled}
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
              disabled={disabled || busy === 'kill'}
              aria-describedby="kill-hint"
              onClick={armKill}
              onKeyDown={(e) => {
                if (e.key === 'Escape') cancelKill();
              }}
            >
              {killPhase === 'confirm' ? t('kill_confirm_button') : t('kill_button')}
            </button>
            {killPhase === 'confirm' && (
              <button type="button" className="btn btn--quiet" onClick={cancelKill}>
                {t('kill_cancel')}
              </button>
            )}
          </div>
          <p id="kill-hint" role={killPhase === 'confirm' ? 'alert' : undefined} className="hint">
            {killPhase === 'confirm' ? t('kill_confirm') : ''}
          </p>
        </Tile>
      </div>
    </div>
  );
}
