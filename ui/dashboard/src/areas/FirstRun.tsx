/**
 * The first start (Spec UX-Neubau §9): three full-screen steps - the person's name, how private
 * Nox should be (Standard preselected), which voice engine speaks. Every step can be skipped and
 * everything can be changed later in Einstellungen; nothing is written before "Weiter".
 *
 * Shown once, on the machine Nox runs on, while no name is set; finishing or skipping remembers
 * that in this browser (`nox.firstRun`), so it never comes back uninvited.
 */

import { useEffect, useId, useRef, useState } from 'react';

import { errorText } from '../../../shared/errors';
import { type Key, type T, optionLabel, privacyLabel } from '../i18n';
import { type IpcClient, api } from '../ipc';
import { PRIVACY_MODES, parseEditableConfig } from '../model';
import { markFirstRunDone } from './firstRunFlag';

const PRIVACY_DESC: Record<string, Key> = {
  full: 'ux_privacy_desc_full',
  balanced: 'ux_privacy_desc_balanced',
  private: 'ux_privacy_desc_private',
  offline: 'ux_privacy_desc_offline',
};

export interface FirstRunProps {
  t: T;
  client: IpcClient | null;
  /** The privacy level in force now: preselected, so "Weiter" never changes it unasked. */
  currentPrivacy?: string | null;
  onDone: () => void;
}

export default function FirstRun({ t, client, currentPrivacy, onDone }: FirstRunProps) {
  const base = useId();
  const [step, setStep] = useState(0);
  const [name, setName] = useState('');
  const startPrivacy = currentPrivacy && (PRIVACY_MODES as readonly string[]).includes(currentPrivacy) ? currentPrivacy : 'balanced';
  const [privacy, setPrivacy] = useState<string>(startPrivacy);
  // "Alles erlaubt" needs an explicit yes (Einstellungen › Privatsphäre); the first run does not
  // offer it unless it is already in force.
  const privacyModes = PRIVACY_MODES.filter((mode) => mode !== 'full' || startPrivacy === 'full');
  const [engine, setEngine] = useState('');
  const [engines, setEngines] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const headingRef = useRef<HTMLHeadingElement | null>(null);

  useEffect(() => {
    if (!client) return;
    let cancelled = false;
    void api
      .configGet(client)
      .then((payload) => {
        const config = parseEditableConfig(payload);
        const spec = config?.schema.find((s) => s.path === 'voice.tts.engine');
        if (cancelled || !config || !spec) return;
        setEngines(spec.options);
        setEngine(String(config.values['voice.tts.engine'] ?? spec.options[0] ?? ''));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [client]);

  useEffect(() => {
    headingRef.current?.focus();
  }, [step]);

  const finish = () => {
    markFirstRunDone();
    onDone();
  };

  /** Saves this step's answer, then moves on; a failure stays on the step and says why. */
  const next = async (save: boolean) => {
    if (save && client) {
      setBusy(true);
      setError('');
      try {
        if (step === 0 && name.trim()) await api.configSet(client, { 'identity.user_display_name': name.trim() });
        if (step === 1 && privacy !== startPrivacy) await api.setPrivacy(client, privacy, '', false);
        if (step === 2 && engine) await api.configSet(client, { 'voice.tts.engine': engine });
      } catch (e) {
        setError(errorText(t('error_prefix'), e));
        setBusy(false);
        return;
      }
      setBusy(false);
    }
    if (step >= 2) finish();
    else setStep(step + 1);
  };

  const titles: Key[] = ['ux_first_name_title', 'ux_first_privacy_title', 'ux_first_voice_title'];

  return (
    <div className="first" role="dialog" aria-modal="true" aria-labelledby={`${base}-h`}>
      <div className="first-in">
        <p className="brand">
          <span className="lamp lamp--brand" aria-hidden="true" />
          {/* The product name, not translated. */}
        <span className="brand-word">Nox</span>
        </p>
        <div className="progress" aria-hidden="true">
          {[0, 1, 2].map((i) => (
            <span key={i} className={i <= step ? 'on' : undefined} />
          ))}
        </div>
        <p className="hint">{`${t('ux_first_step')} ${step + 1} / 3`}</p>
        <h1 id={`${base}-h`} ref={headingRef} tabIndex={-1}>
          {t(titles[step]!)}
        </h1>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void next(true);
          }}
        >
          {step === 0 && (
            <div className="field">
              <label htmlFor={`${base}-name`} className="label">
                {t('ux_first_name_label')}
              </label>
              <input
                id={`${base}-name`}
                className="input"
                autoComplete="given-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
              <p className="hint">{t('ux_first_name_hint')}</p>
            </div>
          )}
          {step === 1 && (
            <fieldset className="choice">
              <legend className="sr-only">{t('ux_first_privacy_title')}</legend>
              {privacyModes.map((mode) => (
                <label key={mode} className={privacy === mode ? 'opt opt--sel' : 'opt'}>
                  <input
                    type="radio"
                    name={`${base}-privacy`}
                    value={mode}
                    checked={privacy === mode}
                    onChange={() => setPrivacy(mode)}
                  />
                  <span className="opt-t">
                    {privacyLabel(t, mode)}
                    {mode === 'balanced' && <span className="badge badge--soft">{t('ux_recommended')}</span>}
                  </span>
                  <span className="opt-d">{t(PRIVACY_DESC[mode]!)}</span>
                </label>
              ))}
            </fieldset>
          )}
          {step === 2 &&
            (engines.length > 0 ? (
              <fieldset className="choice">
                <legend className="sr-only">{t('ux_first_voice_title')}</legend>
                {engines.map((option) => (
                  <label key={option} className={engine === option ? 'opt opt--sel' : 'opt'}>
                    <input
                      type="radio"
                      name={`${base}-voice`}
                      value={option}
                      checked={engine === option}
                      onChange={() => setEngine(option)}
                    />
                    <span className="opt-t">{optionLabel(t, 'voice.tts.engine', option)}</span>
                  </label>
                ))}
              </fieldset>
            ) : (
              <p className="muted">{t('ux_first_voice_unknown')}</p>
            ))}
          <p className="hint">{t('ux_first_later')}</p>
          {error && (
            <p role="alert" className="alert">
              {error}
            </p>
          )}
          <div className="btns first-btns">
            <button type="submit" className="btn btn--primary" disabled={busy || !client}>
              {step === 2 ? t('ux_first_done') : t('ux_first_next')}
            </button>
            <button type="button" className="btn btn--plain" disabled={busy} onClick={() => void next(false)}>
              {t('ux_first_skip')}
            </button>
            <button type="button" className="btn btn--plain" disabled={busy} onClick={finish}>
              {t('ux_first_skip_all')}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
