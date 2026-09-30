/**
 * One step inside the preset editor.
 *
 * Each kind shows only the fields it actually has, because a form that offers a brightness slider
 * on a "wait" step teaches the wrong model of what a step is. The kind selector rebuilds the step
 * from `blankStep`, so switching from a light to a program never leaves stale fields behind that
 * the configuration would then reject.
 */

import { type PresetAction, type PresetStep, type StepKind, STEP_KINDS, blankStep } from '../../model';
import { type T } from '../../i18n';

const MODES = [
  'companion',
  'coding',
  'project',
  'stream',
  'rocket_league',
  'creative',
  'research',
  'focus',
  'idle',
];

export interface StepRowProps {
  t: T;
  step: PresetStep;
  index: number;
  total: number;
  actions: PresetAction[];
  onChange: (step: PresetStep) => void;
  onMove: (delta: number) => void;
  onRemove: () => void;
}

function entitiesField(t: T, step: PresetStep, onChange: (s: PresetStep) => void) {
  return (
    <label className="setting">
      <span className="setting-name">{t('presets_field_entities')}</span>
      <input
        className="input"
        value={(step.entity_ids ?? []).join(', ')}
        onChange={(e) =>
          onChange({
            ...step,
            entity_ids: e.target.value
              .split(',')
              .map((entry) => entry.trim())
              .filter(Boolean),
          })
        }
        placeholder="light.schreibtisch"
        spellCheck={false}
      />
    </label>
  );
}

export function StepRow({ t, step, index, total, actions, onChange, onMove, onRemove }: StepRowProps) {
  return (
    <li className="setting" data-kind={step.kind}>
      <div className="setting-control">
        <label>
          <span className="sr-only">{t('presets_step_kind')}</span>
          <select
            className="select"
            value={step.kind}
            onChange={(e) => onChange(blankStep(e.target.value as StepKind))}
          >
            {STEP_KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {t(`presets_step_${kind}` as Parameters<T>[0])}
              </option>
            ))}
          </select>
        </label>

        <button
          type="button"
          className="btn btn--sm"
          onClick={() => onMove(-1)}
          disabled={index === 0}
          aria-label={t('presets_step_up')}
        >
          ↑
        </button>
        <button
          type="button"
          className="btn btn--sm"
          onClick={() => onMove(1)}
          disabled={index === total - 1}
          aria-label={t('presets_step_down')}
        >
          ↓
        </button>
        <button type="button" className="btn btn--sm" onClick={onRemove}>
          {t('presets_step_remove')}
        </button>
      </div>

      {(step.kind === 'light' || step.kind === 'switch' || step.kind === 'climate') &&
        entitiesField(t, step, onChange)}

      {step.kind === 'scene' && (
        <label className="setting">
          <span className="setting-name">{t('presets_field_scene')}</span>
          <input
            className="input"
            value={step.entity_id ?? ''}
            onChange={(e) => onChange({ ...step, entity_id: e.target.value })}
            placeholder="scene.abend"
            spellCheck={false}
          />
        </label>
      )}

      {(step.kind === 'light' || step.kind === 'switch') && (
        <label className="setting">
          <span className="setting-name">{t('presets_field_on')}</span>
          <input
            type="checkbox"
            checked={step.on === true}
            onChange={(e) => onChange({ ...step, on: e.target.checked })}
          />
        </label>
      )}

      {step.kind === 'light' && (
        <label className="setting">
          <span className="setting-name">{t('presets_field_brightness')}</span>
          <input
            className="input"
            type="number"
            min={0}
            max={100}
            value={step.brightness_pct ?? ''}
            onChange={(e) =>
              onChange({
                ...step,
                brightness_pct: e.target.value === '' ? null : Number(e.target.value),
              })
            }
          />
        </label>
      )}

      {step.kind === 'climate' && (
        <label className="setting">
          <span className="setting-name">{t('presets_field_temperature')}</span>
          <input
            className="input"
            type="number"
            min={4}
            max={35}
            step={0.5}
            value={step.temperature_c ?? 21}
            onChange={(e) => onChange({ ...step, temperature_c: Number(e.target.value) })}
          />
        </label>
      )}

      {step.kind === 'mode' && (
        <label className="setting">
          <span className="setting-name">{t('presets_field_mode')}</span>
          <select
            className="select"
            value={step.mode ?? 'companion'}
            onChange={(e) => onChange({ ...step, mode: e.target.value })}
          >
            {MODES.map((mode) => (
              <option key={mode} value={mode}>
                {mode}
              </option>
            ))}
          </select>
        </label>
      )}

      {step.kind === 'run' && (
        <label className="setting">
          <span className="setting-name">{t('presets_field_action')}</span>
          <select
            className="select"
            value={step.action ?? ''}
            onChange={(e) => onChange({ ...step, action: e.target.value })}
          >
            <option value="">—</option>
            {actions.map((action) => (
              <option key={action.id} value={action.id}>
                {action.name || action.id}
              </option>
            ))}
          </select>
        </label>
      )}

      {step.kind === 'say' && (
        <label className="setting">
          <span className="setting-name">{t('presets_field_text')}</span>
          <input
            className="input"
            value={step.text ?? ''}
            onChange={(e) => onChange({ ...step, text: e.target.value })}
          />
        </label>
      )}

      {step.kind === 'wait' && (
        <label className="setting">
          <span className="setting-name">{t('presets_field_seconds')}</span>
          <input
            className="input"
            type="number"
            min={0.5}
            max={30}
            step={0.5}
            value={step.seconds ?? 1}
            onChange={(e) => onChange({ ...step, seconds: Number(e.target.value) })}
          />
        </label>
      )}
    </li>
  );
}
