/**
 * The editable configuration, group by group, one row per path, in the input type the schema names.
 *
 * Nothing is invented: a path the core does not report is not shown, a type this page cannot render
 * is dropped by the parser, and every option is labelled through `optionLabel` so a select never
 * offers `ptt_only` as if it were a sentence.
 *
 * The "Neustart nötig" note is a group-level line, not a badge on every row. On the voice group it
 * used to appear eleven times out of eleven, which is the same as not appearing at all.
 */

import { useMemo, useState } from 'react';

import { errorText } from '../../../../shared/errors';
import { type T, groupLabel, optionLabel, settingLabel } from '../../i18n';
import { type IpcClient, api } from '../../ipc';
import {
  type ConfigSetResult,
  type EditableConfig,
  type SettingSpec,
  parseConfigSetResult,
} from '../../model';

export interface ConfigFormProps {
  t: T;
  client: IpcClient | null;
  config: EditableConfig;
  onApplied: (values: Record<string, unknown>) => void;
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

export const fieldId = (path: string) => `set-${path.replace(/[^a-zA-Z0-9]+/g, '-')}`;
export const groupId = (group: string) => `settings-group-${group}`;

/** The current editor value for one path: the unsaved draft, else what the core last reported. */
function valueOf(config: EditableConfig, draft: Record<string, unknown>, path: string): unknown {
  return path in draft ? draft[path] : config.values[path];
}

function asText(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (Array.isArray(value)) return value.map((v) => String(v)).join('\n');
  return String(value);
}

export function groupsOf(config: EditableConfig): { group: string; specs: SettingSpec[] }[] {
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
}

export function ConfigForm({ t, client, config, onApplied }: ConfigFormProps) {
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<ConfigSetResult | null>(null);
  const [error, setError] = useState('');

  const groups = useMemo(() => groupsOf(config), [config]);
  const dirty = Object.keys(draft).length > 0;
  const disabled = client === null;

  const setValue = (path: string, value: unknown) => {
    setDraft((d) => ({ ...d, [path]: value }));
    setResult(null);
  };

  const save = async () => {
    if (!client || saving || !dirty) return;
    setSaving(true);
    setError('');
    const sent = { ...draft };
    try {
      const parsed = parseConfigSetResult(await api.configSet(client, sent));
      setResult(parsed);
      // `applied` is the authority. An `ok` answer that names no paths still means "all of them" —
      // otherwise the form would stay dirty forever over a detail of the core's reply.
      const appliedPaths =
        parsed.ok && parsed.applied.length === 0 ? Object.keys(sent) : parsed.applied;
      const applied: Record<string, unknown> = {};
      for (const path of appliedPaths) applied[path] = sent[path];
      onApplied(applied);
      setDraft((d) => {
        const next: Record<string, unknown> = {};
        for (const [path, value] of Object.entries(d)) {
          if (!(path in applied)) next[path] = value;
        }
        return next;
      });
    } catch (e) {
      setError(errorText(t('error_prefix'), e));
    } finally {
      setSaving(false);
    }
  };

  const renderControl = (spec: SettingSpec) => {
    const id = fieldId(spec.path);
    const value = valueOf(config, draft, spec.path);

    switch (spec.type) {
      case 'bool':
        return (
          <span className="switch">
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
          </span>
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
            {spec.options.length === 0 && (
              <option value={asText(value)}>{asText(value) || t('unknown')}</option>
            )}
            {spec.options.map((option) => (
              <option key={option} value={option}>
                {optionLabel(t, spec.path, option)}
              </option>
            ))}
          </select>
        );
      case 'int':
      case 'float':
        return (
          <input
            id={id}
            className="input"
            disabled={disabled}
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
            id={id}
            className="input"
            disabled={disabled}
            type="text"
            value={asText(value)}
            onChange={(e) => setValue(spec.path, e.target.value)}
          />
        );
    }
  };

  return (
    <>
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      {groups.length === 0 && <p className="muted">{t('settings_empty')}</p>}

      {groups.map(({ group, specs }) => {
        const restartGroup = specs.every((s) => s.restartRequired);
        return (
          <div key={group} id={groupId(group)} className="settings-group">
            <h4 className="group-title">{groupLabel(t, group)}</h4>
            {restartGroup && <p className="hint">{t('settings_restart_group')}</p>}
            {specs.map((spec) => {
              const id = fieldId(spec.path);
              const message = result?.errors[spec.path];
              return (
                <div key={spec.path} className="setting">
                  <span className="setting-name">
                    <label htmlFor={id}>{settingLabel(t, spec.path)}</label>
                    {spec.restartRequired && !restartGroup && (
                      <span className="badge badge--warn">{t('settings_restart_badge')}</span>
                    )}
                    {spec.path in draft && (
                      <span className="badge">{t('settings_dirty_badge')}</span>
                    )}
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
        );
      })}

      {groups.length > 0 && (dirty || result) && (
        <div className="savebar">
          <p role="status" aria-live="polite" className="hint">
            {saving
              ? t('settings_saving')
              : dirty
                ? t('settings_dirty')
                : result?.ok
                  ? result.restartRequired.length > 0
                    ? `${t('settings_saved')} ${t('settings_restart_hint')}`
                    : t('settings_saved')
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
    </>
  );
}
