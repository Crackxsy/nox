/**
 * The two choices the header opens: the Modus (what Nox helps with, and - underneath - which
 * security profile it runs under) and the privacy level. Each is a form of its own so the same
 * control also sits on Settings › Privatsphäre, and each is wrapped in a sheet by the header.
 *
 * Choosing is two steps on purpose: the radio picks, the button applies. Arrow keys move through
 * radio buttons and select as they go; applying on every arrow press would switch Nox's profile -
 * or relax its privacy - while the person is still reading the list.
 *
 * Nothing is flipped optimistically. The radios start on what the core reported and the line under
 * them says what the core answered. "Alles erlaubt" is the one level the core only takes with an
 * explicit yes (`confirmed`), so the form asks for it in words before sending it; relaxing privacy
 * asks for the PIN when the core says one is needed.
 */

import { useId, useState } from 'react';

import { isRecord, str } from '../../../shared/guards';
import { refusalText, useIpcAction } from '../hooks';
import { type Key, type T, fill, modeHeadline, modeLabel, privacyLabel, profileLabel } from '../i18n';
import { type IpcClient, IpcError, api } from '../ipc';
import { type CaptureFlags, type DashboardState, PRIVACY_MODES, pinErrorKey } from '../model';
import { MORE_MODES, PRIMARY_MODES, profileForMode } from './presence';

/** What changes in what Nox may do, per security profile (config/profiles/*.yaml), in one sentence. */
const RULES_OF_PROFILE: Record<string, Key> = {
  companion: 'ux_mode_rules_companion',
  stream: 'ux_mode_rules_stream',
  coding: 'ux_mode_rules_coding',
  research: 'ux_mode_rules_research',
};

const MODE_DESC: Record<string, Key> = {
  companion: 'ux_mode_desc_companion',
  stream: 'ux_mode_desc_stream',
  coding: 'ux_mode_desc_coding',
  focus: 'ux_mode_desc_focus',
  idle: 'ux_mode_desc_idle',
  project: 'ux_mode_desc_project',
  creative: 'ux_mode_desc_creative',
  research: 'ux_mode_desc_research',
  rocket_league: 'ux_mode_desc_rocket_league',
};

const PRIVACY_DESC: Record<string, Key> = {
  full: 'ux_privacy_desc_full',
  balanced: 'ux_privacy_desc_balanced',
  private: 'ux_privacy_desc_private',
  offline: 'ux_privacy_desc_offline',
};

/* ------------------------------------------------------------------ mode - */

export interface ModeChoiceProps {
  t: T;
  state: DashboardState;
  client: IpcClient | null;
  device: boolean;
}

export function ModeChoice({ t, state, client, device }: ModeChoiceProps) {
  const name = useId();
  const current = state.mode ?? '';
  const [draft, setDraft] = useState<string | null>(null);
  const [answer, setAnswer] = useState<{ mode: string; profile: string } | null>(null);
  const { busy, error, run } = useIpcAction(client, t, (e) => refusalText(t, e));
  const chosen = draft ?? current;
  const locked = client === null || device;
  const moreOpen = (MORE_MODES as readonly string[]).includes(chosen);

  const apply = () => {
    if (!draft || draft === current) return;
    const mode = draft;
    void run('mode', async (c) => {
      const reply = await api.setMode(c, mode);
      const profile = isRecord(reply) ? str(reply.profile) : '';
      setAnswer({ mode, profile: profile || profileForMode(mode) });
      setDraft(null);
    });
  };

  const option = (mode: string) => (
    <label key={mode} className={chosen === mode ? 'opt opt--sel' : 'opt'}>
      <input
        type="radio"
        name={name}
        value={mode}
        checked={chosen === mode}
        disabled={locked}
        onChange={() => setDraft(mode)}
      />
      <span className="opt-t">{modeLabel(t, mode)}</span>
      <span className="opt-d">{t(MODE_DESC[mode] ?? 'ux_mode_desc_companion')}</span>
      <span className="opt-d opt-r">{t(RULES_OF_PROFILE[profileForMode(mode)] ?? 'ux_mode_rules_companion')}</span>
    </label>
  );

  return (
    <div className="choice-form">
      <fieldset className="choice" disabled={locked}>
        <legend className="sr-only">{t('mode_title')}</legend>
        {PRIMARY_MODES.map(option)}
        <details className="choice-more" open={moreOpen || undefined}>
          <summary>{t('ux_mode_more')}</summary>
          {MORE_MODES.map(option)}
        </details>
      </fieldset>
      {device && <p className="hint">{t('ux_mode_device')}</p>}
      {draft && draft !== current && (
        <div className="btns">
          <button type="button" className="btn btn--primary" disabled={busy === 'mode'} onClick={apply}>
            {fill(t('ux_mode_apply'), modeLabel(t, draft))}
          </button>
          <button type="button" className="btn btn--plain" onClick={() => setDraft(null)}>
            {t('kill_cancel')}
          </button>
        </div>
      )}
      <p role="status" className="hint">
        {answer ? fill(t('ux_mode_now'), modeHeadline(t, answer.mode), profileLabel(t, answer.profile)) : ''}
      </p>
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}
      <details className="tech">
        <summary>{t('perm_technical')}</summary>
        <dl className="tech-list">
          <dt>{t('ux_mode_profile')}</dt>
          <dd>{current ? profileLabel(t, answer?.mode === current ? answer.profile : profileForMode(current)) : t('unknown')}</dd>
          <dt>{t('ux_mode_id')}</dt>
          <dd className="mono">{current || t('unknown')}</dd>
        </dl>
        <p className="hint">{t('ux_mode_profile_hint')}</p>
      </details>
    </div>
  );
}

/** "Stumm": Nox answers in writing only. Allowed on every device. */
export function MuteSwitch({ t, state, client }: { t: T; state: DashboardState; client: IpcClient | null }) {
  const id = useId();
  const { busy, error, run } = useIpcAction(client, t);
  const muted = state.muted === true;
  return (
    <div className="set-row">
      <span className="grow">
        <span className="set-t" id={`${id}-l`}>
          {t('ux_mute')}
        </span>
        <span className="set-d" id={`${id}-d`}>
          {t('ux_mute_desc')}
        </span>
        {error && (
          <span role="alert" className="setting-error">
            {error}
          </span>
        )}
      </span>
      <button
        type="button"
        role="switch"
        className="switch-btn"
        aria-checked={muted}
        aria-labelledby={`${id}-l`}
        aria-describedby={`${id}-d`}
        disabled={client === null || busy === 'mute' || state.muted === null}
        onClick={() => void run('mute', (c) => api.setMuted(c, !muted))}
      />
    </div>
  );
}

/* --------------------------------------------------------------- privacy - */

const CAPTURE_KEYS: (keyof CaptureFlags)[] = ['microphone', 'camera', 'screen', 'cloud'];
const CAPTURE_LABEL: Record<keyof CaptureFlags, Key> = {
  microphone: 'capture_microphone',
  camera: 'capture_camera',
  screen: 'capture_screen',
  cloud: 'capture_cloud',
};

/** "Gerade aktiv: Mikrofon, Bildschirm." - or that nothing is, or that it is not known. */
export function captureLine(t: T, capture: CaptureFlags | null): string {
  if (!capture) return t('ux_capture_unknown');
  const on = CAPTURE_KEYS.filter((k) => capture[k]).map((k) => t(CAPTURE_LABEL[k]));
  return on.length > 0 ? fill(t('ux_capture_active'), on.join(', ')) : t('ux_capture_none');
}

export interface PrivacyChoiceProps {
  t: T;
  state: DashboardState;
  client: IpcClient | null;
  device: boolean;
}

export function PrivacyChoice({ t, state, client, device }: PrivacyChoiceProps) {
  const name = useId();
  const current = state.privacyMode ?? '';
  const [draft, setDraft] = useState<string | null>(null);
  const [pin, setPin] = useState('');
  const [pinNeeded, setPinNeeded] = useState(false);
  const [done, setDone] = useState('');
  const { busy, error, setError, run } = useIpcAction(client, t);
  const chosen = draft ?? current;
  const locked = client === null || device;
  const confirmFull = draft === 'full' && current !== 'full';

  const apply = () => {
    if (!draft || draft === current) return;
    const mode = draft;
    // The PIN goes along only when the core asked for it (a field the person can see).
    const sentPin = pinNeeded ? pin.trim() : '';
    void run('privacy', async (c) => {
      try {
        await api.setPrivacy(c, mode, sentPin, mode === 'full');
        setDraft(null);
        setPin('');
        setPinNeeded(false);
        setDone(fill(t('ux_privacy_now'), privacyLabel(t, mode)));
      } catch (e) {
        const pinKey = e instanceof IpcError ? pinErrorKey(e.code, e.message, sentPin !== '') : null;
        if (pinKey) {
          setPinNeeded(true);
          setPin('');
          setError(t(pinKey));
          return;
        }
        throw e;
      }
    });
  };

  return (
    <div className="choice-form">
      <fieldset className="choice" disabled={locked}>
        <legend className="sr-only">{t('privacy_mode')}</legend>
        {PRIVACY_MODES.map((mode) => (
          <label key={mode} className={chosen === mode ? 'opt opt--sel' : 'opt'}>
            <input
              type="radio"
              name={name}
              value={mode}
              checked={chosen === mode}
              disabled={locked}
              onChange={() => {
                setDraft(mode);
                setPin('');
                setDone('');
              }}
            />
            <span className="opt-t">
              {privacyLabel(t, mode)}
              {mode === 'balanced' && <span className="badge badge--soft">{t('ux_recommended')}</span>}
            </span>
            <span className="opt-d">{t(PRIVACY_DESC[mode] ?? 'ux_privacy_desc_balanced')}</span>
          </label>
        ))}
      </fieldset>
      {device && <p className="hint">{t('ux_privacy_device')}</p>}
      {pinNeeded && draft && (
        <div className="field">
          <label htmlFor={`${name}-pin`} className="label">
            {t('perm_pin')}
          </label>
          <input
            id={`${name}-pin`}
            className="input input--pin"
            type="password"
            inputMode="numeric"
            autoComplete="off"
            value={pin}
            onChange={(e) => setPin(e.target.value)}
          />
        </div>
      )}
      {draft && draft !== current && (
        <>
          {confirmFull && <p className="note note--warn">{t('ux_privacy_full_confirm')}</p>}
          <div className="btns">
            <button
              type="button"
              className="btn btn--primary"
              disabled={busy === 'privacy' || (pinNeeded && pin.trim() === '')}
              onClick={apply}
            >
              {confirmFull ? t('ux_privacy_full_yes') : t('privacy_apply')}
            </button>
            <button
              type="button"
              className="btn btn--plain"
              onClick={() => {
                setDraft(null);
                setPin('');
                setPinNeeded(false);
                setError('');
              }}
            >
              {t('kill_cancel')}
            </button>
          </div>
        </>
      )}
      <p role="status" className="hint">
        {done}
      </p>
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}
      <p className="hint">{captureLine(t, state.capture)}</p>
    </div>
  );
}
