/**
 * Settings page (ST-08 + editable configuration): the schema the core reports via `config.get` is
 * rendered group by group, one row per path, with the input type the schema names. Nothing is
 * invented here — a path the core does not report is not shown, a type this page cannot render is
 * skipped by the parser, and every failed request degrades to a plain "not available" line rather
 * than to an empty form that looks editable.
 *
 * Secrets are write-only: `secrets.status` says whether a name is present, never what it holds, so
 * a stored credential shows "stored" plus Change/Delete and never a masked value that could be
 * copied out. The Twitch device flow shows the user code exactly as the core returned it and polls
 * `twitch.auth.status` at the interval the core dictates.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { type Key, type T, groupLabel, settingLabel, statusLabel, twitchStateLabel } from '../i18n';
import { type IpcClient, api } from '../ipc';
import {
  type ConfigSetResult,
  type EditableConfig,
  type HealthHistoryEntry,
  type SecretStatus,
  type SettingSpec,
  type TwitchAuthStatus,
  type TwitchDeviceCode,
  parseConfigSetResult,
  parseEditableConfig,
  parseHealthHistory,
  parsePersonality,
  parseSecretStatus,
  parseTwitchAuthStatus,
  parseTwitchDeviceCode,
} from '../model';
import { Hero, StateWord, Tile } from '../ui';

export interface SettingsPageProps {
  t: T;
  client: IpcClient | null;
  /** Bumped by `settings.changed`; a change from elsewhere reloads the whole snapshot. */
  revision: number;
  /** Last `twitch.auth.changed` state, so the tile follows an authorisation finished elsewhere. */
  twitchAuthEvent: string | null;
}

/** Groups in reading order; anything the core reports beyond these is appended in schema order. */
const GROUP_ORDER = [
  'identity',
  'voice',
  'privacy',
  'ai',
  'pet',
  'memory',
  'plugins',
  'remote',
  'integrations',
];

const TWITCH_CLIENT_ID = 'nox/twitch/client_id';
const OBS_PASSWORD = 'nox/obs/websocket_password';
const TELEGRAM_TOKEN = 'nox/telegram/bot_token';

const fieldId = (path: string) => `set-${path.replace(/[^a-zA-Z0-9]+/g, '-')}`;

/** The current editor value for one path: the unsaved draft, else what the core last reported. */
function valueOf(config: EditableConfig, draft: Record<string, unknown>, path: string): unknown {
  return path in draft ? draft[path] : config.values[path];
}

function asText(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (Array.isArray(value)) return value.map((v) => String(v)).join('\n');
  return String(value);
}

export function SettingsPage({ t, client, revision, twitchAuthEvent }: SettingsPageProps) {
  const [config, setConfig] = useState<EditableConfig | null>(null);
  const [configFailed, setConfigFailed] = useState(false);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<ConfigSetResult | null>(null);

  const [secrets, setSecrets] = useState<SecretStatus[] | null>(null);
  const [secretsFailed, setSecretsFailed] = useState(false);
  const [secretDraft, setSecretDraft] = useState<Record<string, string>>({});
  const [secretEditing, setSecretEditing] = useState<Record<string, boolean>>({});
  const [secretBusy, setSecretBusy] = useState<string | null>(null);

  const [twitch, setTwitch] = useState<TwitchAuthStatus | null>(null);
  const [twitchFailed, setTwitchFailed] = useState(false);
  const [device, setDevice] = useState<TwitchDeviceCode | null>(null);
  const [twitchBusy, setTwitchBusy] = useState(false);

  const [personality, setPersonality] = useState<{ text: string; path: string } | null>(null);
  const [personalityFailed, setPersonalityFailed] = useState(false);
  const [personalityDraft, setPersonalityDraft] = useState('');
  const [personalitySaved, setPersonalitySaved] = useState(false);

  const [history, setHistory] = useState<HealthHistoryEntry[]>([]);
  const [error, setError] = useState('');

  const disabled = client === null;
  const draftRef = useRef(draft);
  draftRef.current = draft;

  const loadTwitchStatus = useCallback(async (c: IpcClient) => {
    try {
      const parsed = parseTwitchAuthStatus(await api.twitchAuthStatus(c));
      setTwitch(parsed);
      setTwitchFailed(parsed === null);
      return parsed;
    } catch {
      setTwitchFailed(true);
      return null;
    }
  }, []);

  const load = useCallback(
    async (c: IpcClient) => {
      const [cfg, sec, pers, hist] = await Promise.allSettled([
        api.configGet(c),
        api.secretsStatus(c),
        api.personalityGet(c),
        api.healthHistory(c, 30),
      ]);

      const parsedConfig = cfg.status === 'fulfilled' ? parseEditableConfig(cfg.value) : null;
      setConfig(parsedConfig);
      setConfigFailed(parsedConfig === null);

      const parsedSecrets = sec.status === 'fulfilled' ? parseSecretStatus(sec.value) : null;
      setSecrets(parsedSecrets);
      setSecretsFailed(parsedSecrets === null);

      const parsedPersonality = pers.status === 'fulfilled' ? parsePersonality(pers.value) : null;
      setPersonality(parsedPersonality);
      setPersonalityFailed(parsedPersonality === null);
      // Never overwrite something the user is in the middle of typing.
      if (parsedPersonality && personalityDraft === '') setPersonalityDraft(parsedPersonality.text);

      if (hist.status === 'fulfilled') setHistory(parseHealthHistory(hist.value));

      await loadTwitchStatus(c);
    },
    // `personalityDraft` is read, not tracked: reloading must not depend on every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [loadTwitchStatus],
  );

  useEffect(() => {
    if (client) void load(client);
  }, [client, revision, load]);

  useEffect(() => {
    if (client && twitchAuthEvent) void loadTwitchStatus(client);
  }, [client, twitchAuthEvent, loadTwitchStatus]);

  // Device flow: poll at exactly the interval the core dictated, until it settles.
  useEffect(() => {
    if (!client || !device) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void loadTwitchStatus(client).then((status) => {
        if (cancelled || !status) return;
        if (status.state !== 'pending') setDevice(null);
      });
    }, device.interval * 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [client, device, loadTwitchStatus]);

  const groups = useMemo(() => {
    if (!config) return [];
    const byGroup = new Map<string, SettingSpec[]>();
    for (const spec of config.schema) {
      const list = byGroup.get(spec.group);
      if (list) list.push(spec);
      else byGroup.set(spec.group, [spec]);
    }
    const ordered = [
      ...GROUP_ORDER.filter((g) => byGroup.has(g)),
      ...[...byGroup.keys()].filter((g) => !GROUP_ORDER.includes(g)),
    ];
    return ordered.map((group) => ({ group, specs: byGroup.get(group) ?? [] }));
  }, [config]);

  const dirty = Object.keys(draft).length > 0;

  const setValue = (path: string, value: unknown) => {
    setDraft((d) => ({ ...d, [path]: value }));
    setResult(null);
  };

  const save = async () => {
    if (!client || !config) return;
    setSaving(true);
    setError('');
    try {
      const sent = Object.keys(draftRef.current);
      const parsed = parseConfigSetResult(await api.configSet(client, draftRef.current));
      setResult(parsed);
      // `applied` is the authority. An `ok` answer that names no paths still means "all of them" —
      // otherwise the form would stay dirty forever over a detail of the core's reply.
      const applied = parsed.ok && parsed.applied.length === 0 ? sent : parsed.applied;
      // Applied paths are now the core's truth; only what it rejected stays in the draft.
      setConfig((c) =>
        c
          ? {
              ...c,
              values: applied.reduce(
                (acc, path) => ({ ...acc, [path]: draftRef.current[path] }),
                { ...c.values },
              ),
            }
          : c,
      );
      setDraft((d) => {
        const next: Record<string, unknown> = {};
        for (const [path, value] of Object.entries(d)) {
          if (!applied.includes(path)) next[path] = value;
        }
        return next;
      });
    } catch (e) {
      setError(`${t('error_prefix')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSaving(false);
    }
  };

  const secretPresent = (name: string) => secrets?.some((s) => s.name === name && s.present) ?? false;

  const saveSecret = async (name: string) => {
    const value = secretDraft[name] ?? '';
    if (!client || value.trim() === '') return;
    setSecretBusy(name);
    setError('');
    try {
      await api.secretSet(client, name, value.trim());
      setSecretDraft((d) => ({ ...d, [name]: '' }));
      setSecretEditing((d) => ({ ...d, [name]: false }));
      const parsed = parseSecretStatus(await api.secretsStatus(client));
      setSecrets(parsed);
      setSecretsFailed(parsed === null);
    } catch (e) {
      setError(`${t('error_prefix')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSecretBusy(null);
    }
  };

  const deleteSecret = async (name: string) => {
    if (!client) return;
    setSecretBusy(name);
    setError('');
    try {
      await api.secretDelete(client, name);
      const parsed = parseSecretStatus(await api.secretsStatus(client));
      setSecrets(parsed);
      setSecretsFailed(parsed === null);
    } catch (e) {
      setError(`${t('error_prefix')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSecretBusy(null);
    }
  };

  const connectTwitch = async () => {
    if (!client) return;
    setTwitchBusy(true);
    setError('');
    try {
      const parsed = parseTwitchDeviceCode(await api.twitchAuthStart(client));
      setDevice(parsed);
      if (parsed) await loadTwitchStatus(client);
    } catch (e) {
      setError(`${t('error_prefix')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setTwitchBusy(false);
    }
  };

  const disconnectTwitch = async () => {
    if (!client) return;
    setTwitchBusy(true);
    setError('');
    try {
      await api.twitchAuthDisconnect(client);
      setDevice(null);
      await loadTwitchStatus(client);
    } catch (e) {
      setError(`${t('error_prefix')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setTwitchBusy(false);
    }
  };

  const savePersonality = async () => {
    if (!client) return;
    setError('');
    setPersonalitySaved(false);
    try {
      await api.personalitySet(client, personalityDraft);
      setPersonality((p) => (p ? { ...p, text: personalityDraft } : p));
      setPersonalitySaved(true);
    } catch (e) {
      setError(`${t('error_prefix')}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const renderControl = (spec: SettingSpec) => {
    if (!config) return null;
    const id = fieldId(spec.path);
    const value = valueOf(config, draft, spec.path);
    const common = { id, disabled, className: 'input' } as const;

    switch (spec.type) {
      case 'bool':
        return (
          <label className="switch" htmlFor={id}>
            <input
              id={id}
              type="checkbox"
              checked={value === true}
              disabled={disabled}
              onChange={(e) => setValue(spec.path, e.target.checked)}
            />
            <span className="switch-track" aria-hidden="true">
              <span className="switch-knob" />
            </span>
          </label>
        );
      case 'enum':
        return (
          <select
            id={id}
            className="select"
            disabled={disabled}
            value={asText(value)}
            onChange={(e) => setValue(spec.path, e.target.value)}
          >
            {spec.options.length === 0 && <option value={asText(value)}>{asText(value)}</option>}
            {spec.options.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        );
      case 'int':
      case 'float':
        return (
          <input
            {...common}
            type="number"
            inputMode={spec.type === 'int' ? 'numeric' : 'decimal'}
            step={spec.type === 'int' ? 1 : 'any'}
            min={spec.min ?? undefined}
            max={spec.max ?? undefined}
            value={asText(value)}
            onChange={(e) => {
              const raw = e.target.value;
              const parsed = spec.type === 'int' ? parseInt(raw, 10) : parseFloat(raw);
              setValue(spec.path, raw === '' || Number.isNaN(parsed) ? raw : parsed);
            }}
          />
        );
      case 'list[str]':
        return (
          <textarea
            id={id}
            className="textarea"
            rows={4}
            disabled={disabled}
            aria-describedby={`${id}-hint`}
            value={asText(value)}
            onChange={(e) =>
              setValue(
                spec.path,
                e.target.value
                  .split('\n')
                  .map((line) => line.trim())
                  .filter((line) => line.length > 0),
              )
            }
          />
        );
      default:
        return (
          <input
            {...common}
            type="text"
            value={asText(value)}
            onChange={(e) => setValue(spec.path, e.target.value)}
          />
        );
    }
  };

  const secretField = (name: string, labelKey: Key, hintKey?: Key) => {
    const present = secretPresent(name);
    const editing = secretEditing[name] === true;
    const id = fieldId(name);
    if (present && !editing) {
      return (
        <div className="field field--spaced">
          <span className="label">{t(labelKey)}</span>
          <div className="tile-actions">
            <StateWord status="available" label={t('secret_stored')} />
            <button
              type="button"
              className="link"
              disabled={disabled || secretBusy === name}
              onClick={() => setSecretEditing((d) => ({ ...d, [name]: true }))}
            >
              {t('secret_change')}
            </button>
            <button
              type="button"
              className="link link--plain"
              disabled={disabled || secretBusy === name}
              onClick={() => void deleteSecret(name)}
            >
              {t('secret_delete')}
            </button>
          </div>
        </div>
      );
    }
    return (
      <div className="field field--spaced">
        <label htmlFor={id} className="label">
          {t(labelKey)}
        </label>
        <input
          id={id}
          type="password"
          className="input"
          autoComplete="off"
          disabled={disabled || secretBusy === name}
          aria-describedby={hintKey ? `${id}-hint` : undefined}
          value={secretDraft[name] ?? ''}
          onChange={(e) => setSecretDraft((d) => ({ ...d, [name]: e.target.value }))}
        />
        {hintKey && (
          <p id={`${id}-hint`} className="hint">
            {t(hintKey)}
          </p>
        )}
        <div className="tile-actions">
          <button
            type="button"
            className="btn btn--sm"
            disabled={disabled || secretBusy === name || (secretDraft[name] ?? '').trim() === ''}
            onClick={() => void saveSecret(name)}
          >
            {t('secret_save')}
          </button>
          {present && (
            <button
              type="button"
              className="link link--plain"
              onClick={() => setSecretEditing((d) => ({ ...d, [name]: false }))}
            >
              {t('secret_cancel')}
            </button>
          )}
        </div>
      </div>
    );
  };

  const twitchState = twitch?.state ?? 'idle';

  return (
    <div className="page wrap">
      <Hero
        title={t('hero_settings')}
        sub={t('hero_settings_sub')}
        links={
          <button
            type="button"
            className="link"
            disabled={disabled}
            onClick={() => client && void load(client)}
          >
            {t('settings_refresh')}
          </button>
        }
      >
        {config?.userConfigPath && (
          <p className="hero-sub mono">
            {t('settings_file')}: {config.userConfigPath}
          </p>
        )}
      </Hero>

      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      <section aria-labelledby="h-config">
        <div className="rail-head">
          <div>
            <h3 id="h-config" className="rail-title">
              {t('settings_title')}
            </h3>
            {result?.restartRequired.length ? (
              <p className="rail-sub">{t('settings_restart_hint')}</p>
            ) : null}
          </div>
        </div>

        {configFailed && <p className="muted">{t('settings_unavailable')}</p>}
        {config && groups.length === 0 && <p className="muted">{t('settings_empty')}</p>}

        {config &&
          groups.map(({ group, specs }) => (
            <div key={group} className="settings-group">
              <h4 className="group-title">{groupLabel(t, group)}</h4>
              {specs.map((spec) => {
                const id = fieldId(spec.path);
                const message = result?.errors[spec.path];
                const label = settingLabel(t, spec.path);
                return (
                  <div key={spec.path} className="setting">
                    <span className="setting-name">
                      <label htmlFor={id}>{label}</label>
                      {spec.restartRequired && (
                        <span className="badge badge--warn">{t('settings_restart_badge')}</span>
                      )}
                      {spec.path in draft && <span className="badge">{t('settings_dirty_badge')}</span>}
                    </span>
                    <span className="setting-control">
                      {renderControl(spec)}
                      {spec.type === 'list[str]' && (
                        <span id={`${id}-hint`} className="hint">
                          {t('settings_list_hint')}
                        </span>
                      )}
                    </span>
                    {message && (
                      <span role="alert" className="setting-error">
                        {message}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          ))}

        {config && groups.length > 0 && (dirty || result) && (
          <div className="savebar">
            <p role="status" aria-live="polite" className="hint">
              {saving
                ? t('settings_saving')
                : dirty
                  ? t('settings_dirty')
                  : result?.ok
                    ? t('settings_saved')
                    : ''}
            </p>
            <span className="savebar-actions">
              {dirty && (
                <button
                  type="button"
                  className="link link--plain"
                  onClick={() => {
                    setDraft({});
                    setResult(null);
                  }}
                >
                  {t('settings_discard')}
                </button>
              )}
              <button
                type="button"
                className="btn"
                disabled={disabled || saving || !dirty}
                onClick={() => void save()}
              >
                {saving ? t('settings_saving') : t('settings_save')}
              </button>
            </span>
          </div>
        )}
      </section>

      <section aria-labelledby="h-integrations">
        <div className="rail-head">
          <div>
            <h3 id="h-integrations" className="rail-title">
              {t('settings_group_integrations')}
            </h3>
            <p className="rail-sub">{t('integrations_hint')}</p>
          </div>
        </div>

        {secretsFailed ? (
          <p className="muted">{t('secrets_unavailable')}</p>
        ) : (
          <div className="tiles tiles--single">
            <Tile
              feature
              eyebrow={twitchStateLabel(t, twitchState)}
              title={t('stream_plugin_twitch')}
              lede={secretPresent(TWITCH_CLIENT_ID) ? undefined : t('twitch_setup_title')}
            >
              {!secretPresent(TWITCH_CLIENT_ID) && (
                <>
                  <ol className="steps">
                    <li>{t('twitch_step_1')}</li>
                    <li>{t('twitch_step_2')}</li>
                    <li>{t('twitch_step_3')}</li>
                  </ol>
                  {secretField(TWITCH_CLIENT_ID, 'twitch_client_id_label')}
                </>
              )}

              {secretPresent(TWITCH_CLIENT_ID) && (
                <>
                  {twitchFailed && <p className="muted">{t('secrets_unavailable')}</p>}

                  {twitchState === 'authorized' ? (
                    <>
                      <dl className="facts">
                        <dt>{t('twitch_account')}</dt>
                        <dd>{twitch?.login || t('unknown')}</dd>
                        {twitch?.expiresAt ? (
                          <>
                            <dt>{t('remote_expires_at')}</dt>
                            <dd className="break">{twitch.expiresAt}</dd>
                          </>
                        ) : null}
                      </dl>
                      <div className="tile-actions">
                        <button
                          type="button"
                          className="btn btn--quiet"
                          disabled={disabled || twitchBusy}
                          onClick={() => void disconnectTwitch()}
                        >
                          {t('twitch_disconnect')}
                        </button>
                      </div>
                    </>
                  ) : device ? (
                    <div role="status" aria-live="polite">
                      <p className="label">{t('twitch_code_label')}</p>
                      <p className="code-display">{device.userCode}</p>
                      <p className="hint">{t('twitch_code_hint')}</p>
                      <div className="tile-actions">
                        <a
                          className="btn"
                          href={device.verificationUri}
                          target="_blank"
                          rel="noreferrer noopener"
                        >
                          {t('twitch_open_link')}
                        </a>
                      </div>
                    </div>
                  ) : (
                    <>
                      {twitch?.error && <p className="hint break">{twitch.error}</p>}
                      <div className="tile-actions">
                        <button
                          type="button"
                          className="btn"
                          disabled={disabled || twitchBusy}
                          onClick={() => void connectTwitch()}
                        >
                          {twitchBusy ? t('twitch_connecting') : t('twitch_connect')}
                        </button>
                      </div>
                    </>
                  )}
                </>
              )}
            </Tile>
          </div>
        )}

        {!secretsFailed && (
          <div className="tiles tiles--pair">
            <Tile eyebrow={t('settings_group_integrations')} title={t('stream_plugin_obs')}>
              {secretField(OBS_PASSWORD, 'obs_password_label', 'obs_hint')}
            </Tile>

            <Tile eyebrow={t('settings_group_integrations')} title="Telegram">
              {secretField(TELEGRAM_TOKEN, 'telegram_token_label', 'telegram_hint')}
            </Tile>
          </div>
        )}
      </section>

      <div className="tiles tiles--single">
        <Tile
          id="personality"
          eyebrow={t('settings_group_identity')}
          title={t('personality_title')}
          lede={t('personality_hint')}
        >
          {personalityFailed ? (
            <p className="muted">{t('personality_unavailable')}</p>
          ) : (
            <>
              <div className="field">
                <label htmlFor="personality-text" className="label">
                  {t('personality_label')}
                </label>
                <textarea
                  id="personality-text"
                  className="textarea"
                  rows={12}
                  disabled={disabled}
                  value={personalityDraft}
                  onChange={(e) => {
                    setPersonalityDraft(e.target.value);
                    setPersonalitySaved(false);
                  }}
                />
              </div>
              <div className="tile-actions">
                <button
                  type="button"
                  className="btn"
                  disabled={disabled || personalityDraft === (personality?.text ?? '')}
                  onClick={() => void savePersonality()}
                >
                  {t('personality_save')}
                </button>
                {personalitySaved && (
                  <p role="status" className="ok-note">
                    {t('personality_saved')}
                  </p>
                )}
                {personality?.path && <span className="hint mono break">{personality.path}</span>}
              </div>
            </>
          )}
        </Tile>
      </div>

      <section aria-labelledby="h-health-history">
        <div className="rail-head">
          <div>
            <h3 id="h-health-history" className="rail-title">
              {t('settings_health_title')}
            </h3>
            <p className="rail-sub">{t('settings_health_hint')}</p>
          </div>
        </div>
        {history.length === 0 ? (
          <p className="muted">{t('settings_health_empty')}</p>
        ) : (
          <div className="table-scroll">
            <table className="specs">
              <caption className="sr-only">{t('settings_health_title')}</caption>
              <thead>
                <tr>
                  <th scope="col">{t('settings_col_component')}</th>
                  <th scope="col">{t('settings_col_status')}</th>
                  <th scope="col">{t('settings_col_time')}</th>
                  <th scope="col">{t('settings_col_reason')}</th>
                </tr>
              </thead>
              <tbody>
                {history.map((h) => (
                  <tr key={h.id}>
                    <th scope="row">{h.component || '–'}</th>
                    <td>
                      <StateWord status={h.status} label={statusLabel(t, h.status)} />
                    </td>
                    <td className="muted nowrap">{h.ts || '–'}</td>
                    <td className="muted break">{h.reason || '–'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
