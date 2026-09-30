/**
 * The editor for one preset: what it is called, how it starts, and what it does.
 *
 * Phrases are edited as one comma-separated line rather than as a list of inputs. A preset
 * usually has two or three of them and they are short, so a single line is both quicker to change
 * and easier to read back than a column of boxes with remove buttons.
 */

import {
  type Preset,
  type PresetAction,
  type PresetStep,
  type StepKind,
  blankStep,
} from '../../model';
import { type T } from '../../i18n';
import { StepRow } from './StepRow';

const WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] as const;

export interface PresetFormProps {
  t: T;
  preset: Preset;
  actions: PresetAction[];
  onChange: (preset: Preset) => void;
}

export function PresetForm({ t, preset, actions, onChange }: PresetFormProps) {
  const setTriggers = (changes: Partial<Preset['triggers']>) =>
    onChange({ ...preset, triggers: { ...preset.triggers, ...changes } });

  const setStep = (index: number, step: PresetStep) => {
    const steps = [...preset.steps];
    steps[index] = step;
    onChange({ ...preset, steps });
  };

  const moveStep = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= preset.steps.length) return;
    const steps = [...preset.steps];
    const moved = steps[index];
    const other = steps[target];
    if (!moved || !other) return;
    steps[index] = other;
    steps[target] = moved;
    onChange({ ...preset, steps });
  };

  const toggleDay = (day: string) => {
    const days = preset.triggers.days.includes(day)
      ? preset.triggers.days.filter((entry) => entry !== day)
      : [...preset.triggers.days, day];
    setTriggers({ days });
  };

  return (
    <div className="settings-group">
      <label className="setting">
        <span className="setting-name">{t('presets_name')}</span>
        <input
          className="input"
          value={preset.name}
          onChange={(e) => onChange({ ...preset, name: e.target.value })}
          placeholder="Gaming"
        />
      </label>

      <label className="setting">
        <span className="setting-name">{t('presets_phrases')}</span>
        <input
          className="input"
          value={preset.triggers.phrases.join(', ')}
          onChange={(e) =>
            setTriggers({
              phrases: e.target.value
                .split(',')
                .map((entry) => entry.trim())
                .filter(Boolean),
            })
          }
          placeholder="gaming mode, zocken"
        />
      </label>
      <p className="hint">{t('presets_phrases_hint')}</p>

      <label className="setting">
        <span className="setting-name">{t('presets_at')}</span>
        <input
          className="input"
          type="time"
          value={preset.triggers.at}
          onChange={(e) => setTriggers({ at: e.target.value })}
        />
      </label>
      <p className="hint">{t('presets_at_hint')}</p>

      {preset.triggers.at !== '' && (
        <fieldset className="setting">
          <legend className="setting-name">{t('presets_days')}</legend>
          <div className="setting-control">
            {WEEKDAYS.map((day) => (
              <label key={day} className="badge">
                <input
                  type="checkbox"
                  checked={preset.triggers.days.includes(day)}
                  onChange={() => toggleDay(day)}
                />{' '}
                {t(`weekday_${day}` as Parameters<T>[0])}
              </label>
            ))}
          </div>
        </fieldset>
      )}

      <label className="setting">
        <span className="setting-name">{t('presets_on_process_start')}</span>
        <input
          className="input"
          value={preset.triggers.on_process_start}
          onChange={(e) => setTriggers({ on_process_start: e.target.value })}
          placeholder="RocketLeague.exe"
          spellCheck={false}
        />
      </label>

      <label className="setting">
        <span className="setting-name">{t('presets_on_process_end')}</span>
        <input
          className="input"
          value={preset.triggers.on_process_end}
          onChange={(e) => setTriggers({ on_process_end: e.target.value })}
          spellCheck={false}
        />
      </label>

      <h4 className="group-title">{t('presets_steps')}</h4>
      <ol className="settings-group">
        {preset.steps.map((step, index) => (
          <StepRow
            key={index}
            t={t}
            step={step}
            index={index}
            total={preset.steps.length}
            actions={actions}
            onChange={(next) => setStep(index, next)}
            onMove={(delta) => moveStep(index, delta)}
            onRemove={() =>
              onChange({ ...preset, steps: preset.steps.filter((_, i) => i !== index) })
            }
          />
        ))}
      </ol>

      <div className="setting-control">
        <select
          className="select"
          value=""
          onChange={(e) => {
            if (!e.target.value) return;
            onChange({ ...preset, steps: [...preset.steps, blankStep(e.target.value as StepKind)] });
          }}
        >
          <option value="">{t('presets_step_add')}</option>
          {(['light', 'switch', 'scene', 'climate', 'mode', 'run', 'say', 'wait'] as StepKind[]).map(
            (kind) => (
              <option key={kind} value={kind}>
                {t(`presets_step_${kind}` as Parameters<T>[0])}
              </option>
            ),
          )}
        </select>
      </div>
    </div>
  );
}
