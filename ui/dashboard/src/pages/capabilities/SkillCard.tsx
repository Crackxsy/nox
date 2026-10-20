/**
 * One skill Nox taught itself: what it is for, how far it got, and — once it is built — exactly
 * what activating it would allow: its tools with their risk, the hosts it may reach (new ones
 * marked), the secrets it needs, and how its tests went.
 *
 * Activation is one button. The PIN field appears only after the core answered `needs_pin`; it is
 * not shown up front, because most skills do not need it and a PIN field on every card would teach
 * people to type the PIN without reading. Tool descriptions were written by the model that built
 * the skill, so they are rendered as plain text and labelled as such.
 */

import { useState } from 'react';

import { formatTimestamp, timestampTitle } from '../../../../shared/format';
import type { Lang } from '../../../../shared/lang';
import { type Key, type T, fill } from '../../i18n';
import { type ActionResult, type Skill, type SkillState, missingSecrets } from '../../model';
import { StateWord, Tile, type Tone } from '../../ui';
import { ConfirmAction, PinField } from './shared';

const STATE_KEYS: Record<SkillState, Key> = {
  building: 'caps_skill_state_building',
  ready: 'caps_skill_state_ready',
  failed: 'caps_skill_state_failed',
  active: 'caps_skill_state_active',
  inactive: 'caps_skill_state_inactive',
};

const STATE_TONES: Record<SkillState, Tone> = {
  building: 'warn',
  ready: 'warn',
  failed: 'danger',
  active: 'ok',
  inactive: 'off',
};

const RISK_KEYS: Record<string, Key> = {
  low: 'caps_risk_low',
  medium: 'caps_risk_medium',
  high: 'caps_risk_high',
  critical: 'caps_risk_critical',
};

const RISK_TONES: Record<string, Tone> = {
  low: 'ok',
  medium: 'warn',
  high: 'danger',
  critical: 'danger',
};

function isSkillState(state: string): state is SkillState {
  return state in STATE_KEYS;
}

export interface SkillCardProps {
  t: T;
  lang: Lang;
  skill: Skill;
  busy: string | null;
  connected: boolean;
  pinConfigured: boolean;
  /** Resolves with the core's answer, or `null` when the request itself failed. */
  onActivate: (id: string, pin: string) => Promise<ActionResult | null>;
  onDeactivate: (id: string) => void;
  onForget: (id: string) => void;
  /** Resolves with `''` on success, else the sentence to show under the field. */
  onSetSecret: (name: string, value: string, pin: string) => Promise<string>;
}

export function SkillCard({
  t,
  lang,
  skill,
  busy,
  connected,
  pinConfigured,
  onActivate,
  onDeactivate,
  onForget,
  onSetSecret,
}: SkillCardProps) {
  const [pinAsked, setPinAsked] = useState(false);
  const [pin, setPin] = useState('');
  const [refusal, setRefusal] = useState('');
  const [secretDraft, setSecretDraft] = useState<Record<string, string>>({});
  const [secretPin, setSecretPin] = useState('');
  const [secretError, setSecretError] = useState<Record<string, string>>({});

  const known = isSkillState(skill.state);
  const stateLabel = known ? t(STATE_KEYS[skill.state as SkillState]) : skill.state;
  const tone: Tone = known ? STATE_TONES[skill.state as SkillState] : 'off';
  const disabled = !connected || busy !== null;
  const summary = skill.summary;
  const missing = missingSecrets(summary);
  const canActivate = skill.state === 'ready' || skill.state === 'inactive';
  const domId = `skill-${skill.id}`;

  const activate = async () => {
    setRefusal('');
    const result = await onActivate(skill.id, pinAsked ? pin : '');
    setPin('');
    if (!result) return;
    if (result.needsPin) {
      setPinAsked(true);
      setRefusal(result.reason);
      return;
    }
    if (!result.ok) setRefusal(fill(t('caps_refused'), result.reason || t('unknown')));
    else setPinAsked(false);
  };

  const saveSecret = async (name: string) => {
    const value = secretDraft[name] ?? '';
    if (value === '') return;
    const message = await onSetSecret(name, value, pinConfigured ? secretPin : '');
    setSecretPin('');
    setSecretError((m) => ({ ...m, [name]: message }));
    if (!message) setSecretDraft((d) => ({ ...d, [name]: '' }));
  };

  return (
    <Tile level={4} id={domId} title={skill.intent || skill.id}>
      <p className="caps-meta">
        <StateWord tone={tone} label={stateLabel} />
        <span className="mono">{skill.id}</span>
        {skill.version > 0 && <span className="hint">{fill(t('caps_skill_version'), skill.version)}</span>}
        {skill.updatedAt && (
          <span className="hint" title={timestampTitle(skill.updatedAt)}>
            {fill(t('caps_skill_updated'), formatTimestamp(skill.updatedAt, lang))}
          </span>
        )}
      </p>

      {skill.state === 'building' && (
        <p role="status" aria-live="polite">
          {fill(t('caps_skill_building'), Math.max(1, skill.rounds))}
          {skill.message && <span className="hint caps-block">{skill.message}</span>}
        </p>
      )}

      {skill.state === 'failed' && (
        <>
          {skill.message && <p className="alert">{skill.message}</p>}
          {skill.problems.length > 0 && (
            <>
              <p className="subhead">{t('caps_skill_problems')}</p>
              <ul className="caps-bullets">
                {skill.problems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            </>
          )}
        </>
      )}

      {summary && skill.state !== 'building' && (
        <>
          <p className="subhead">{t('caps_skill_tools')}</p>
          {summary.tools.length === 0 ? (
            <p className="hint">{t('caps_skill_tools_none')}</p>
          ) : (
            <ul className="caps-tools">
              {summary.tools.map((tool) => (
                <li key={tool.name} className="caps-tool">
                  <span className="caps-tool-name mono">{tool.name}</span>{' '}
                  <StateWord
                    tone={RISK_TONES[tool.risk] ?? 'off'}
                    label={RISK_KEYS[tool.risk] ? t(RISK_KEYS[tool.risk] as Key) : tool.risk}
                  />
                  {tool.description && (
                    <p className="caps-untrusted">
                      <span className="caps-untrusted-label">{t('caps_skill_untrusted')}</span>
                      <span className="caps-untrusted-text">{tool.description}</span>
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}

          <p className="subhead">{t('caps_skill_hosts')}</p>
          {summary.hosts.length === 0 ? (
            <p className="hint">{t('caps_skill_hosts_none')}</p>
          ) : (
            <ul className="caps-bullets">
              {summary.hosts.map((host) => (
                <li key={host}>
                  <code className="mono">{host}</code>{' '}
                  {summary.newHosts.includes(host) && (
                    <span className="badge badge--warn">{t('caps_skill_host_new')}</span>
                  )}
                </li>
              ))}
            </ul>
          )}

          {summary.secrets.length > 0 && (
            <>
              <p className="subhead">{t('caps_skill_secrets')}</p>
              <ul className="caps-bullets">
                {summary.secrets.map((name) => {
                  const present = summary.secretsPresent[name] === true;
                  const fieldId = `${domId}-secret-${name.replace(/[^a-zA-Z0-9]+/g, '-')}`;
                  return (
                    <li key={name}>
                      <code className="mono">{name}</code>{' '}
                      <StateWord
                        tone={present ? 'ok' : 'warn'}
                        label={t(present ? 'caps_skill_secret_stored' : 'caps_skill_secret_missing')}
                      />
                      {!present && (
                        <div className="caps-inline-form">
                          <div className="field">
                            <label htmlFor={fieldId} className="label">
                              {fill(t('caps_skill_secret_value'), name)}
                            </label>
                            <input
                              id={fieldId}
                              type="password"
                              className="input"
                              autoComplete="off"
                              disabled={disabled}
                              value={secretDraft[name] ?? ''}
                              onChange={(e) =>
                                setSecretDraft((d) => ({ ...d, [name]: e.target.value }))
                              }
                            />
                          </div>
                          <button
                            type="button"
                            className="btn btn--sm"
                            disabled={disabled || (secretDraft[name] ?? '') === ''}
                            onClick={() => void saveSecret(name)}
                          >
                            {t('caps_skill_secret_save')}
                          </button>
                          {secretError[name] && (
                            <p role="alert" className="setting-error">
                              {secretError[name]}
                            </p>
                          )}
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
              {pinConfigured && missing.length > 0 && (
                <PinField
                  t={t}
                  id={`${domId}-secret-pin`}
                  value={secretPin}
                  disabled={disabled}
                  onChange={setSecretPin}
                  hint={t('pin_hint')}
                />
              )}
            </>
          )}

          <p className="subhead">{t('caps_skill_tests')}</p>
          <p>
            <StateWord
              tone={summary.testsFailed > 0 ? 'danger' : summary.testsPassed > 0 ? 'ok' : 'off'}
              label={fill(t('caps_skill_tests_result'), summary.testsPassed, summary.testsFailed)}
            />
          </p>
          {/* A status word stays one line; the caveat is a sentence and wraps on a phone. */}
          <p className="hint">{t('caps_skill_tests_caveat')}</p>
          {summary.testOutput && (
            <details className="caps-details">
              <summary>{t('caps_skill_test_output')}</summary>
              <pre className="caps-pre">{summary.testOutput}</pre>
            </details>
          )}
        </>
      )}

      {canActivate && missing.length > 0 && <p className="hint">{t('caps_skill_secrets_first')}</p>}

      {pinAsked && canActivate && (
        <PinField
          t={t}
          id={`${domId}-pin`}
          value={pin}
          disabled={disabled}
          onChange={setPin}
          hint={t('caps_skill_pin_hint')}
        />
      )}
      {refusal && (
        <p role="alert" className="alert">
          {refusal}
        </p>
      )}

      <div className="tile-actions">
        {canActivate && (
          <button
            type="button"
            className="btn"
            disabled={disabled || (pinAsked && pin === '')}
            onClick={() => void activate()}
          >
            {t('caps_skill_activate')}
          </button>
        )}
        {skill.state === 'active' && (
          <button
            type="button"
            className="btn btn--quiet btn--sm"
            disabled={disabled}
            onClick={() => onDeactivate(skill.id)}
          >
            {t('caps_skill_deactivate')}
          </button>
        )}
        <ConfirmAction
          t={t}
          label={t('caps_skill_forget')}
          question={fill(t('caps_skill_forget_confirm'), skill.intent || skill.id)}
          confirmLabel={t('caps_skill_forget_yes')}
          disabled={disabled}
          onConfirm={() => onForget(skill.id)}
        />
      </div>
    </Tile>
  );
}
