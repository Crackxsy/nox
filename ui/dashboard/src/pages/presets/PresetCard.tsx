/**
 * One preset on the page: what it does, a Run button, and what happened the last time it ran.
 *
 * The result is shown on the card rather than in a banner, and it is shown per step. A run where
 * the lamp was unreachable but the program started is not a failure and not a success, and the
 * only way to say that honestly is to name the step that did not work.
 */

import { type T } from '../../i18n';
import { type Preset, type PresetAction, type PresetRunResult } from '../../model';
import { Detail, StateWord, Tile } from '../../ui';
import { PresetForm } from './PresetForm';

export interface PresetCardProps {
  t: T;
  preset: Preset;
  actions: PresetAction[];
  run: PresetRunResult | undefined;
  busy: string | null;
  editing: Preset | null;
  connected: boolean;
  onEdit: (preset: Preset | null) => void;
  onSave: (preset: Preset) => void;
  onDelete: (id: string) => void;
  onRun: (id: string) => void;
}

function stepNames(t: T, preset: Preset): string {
  return preset.steps.map((step) => t(`presets_step_${step.kind}` as Parameters<T>[0])).join(' · ');
}

function triggerLine(preset: Preset): string {
  const parts: string[] = [];
  if (preset.triggers.phrases.length > 0) {
    parts.push(preset.triggers.phrases.map((phrase) => `„${phrase}“`).join(', '));
  }
  if (preset.triggers.at) {
    const days = preset.triggers.days.length > 0 ? ` (${preset.triggers.days.join(', ')})` : '';
    parts.push(`${preset.triggers.at}${days}`);
  }
  if (preset.triggers.on_process_start) parts.push(`▶ ${preset.triggers.on_process_start}`);
  if (preset.triggers.on_process_end) parts.push(`■ ${preset.triggers.on_process_end}`);
  return parts.join(' · ');
}

function runLabel(t: T, run: PresetRunResult): string {
  const failures = run.steps.filter((step) => !step.ok).length;
  if (failures === 0) return t('presets_run_ok');
  return failures === 1 ? t('presets_run_partial') : t('presets_run_partial_many');
}

export function PresetCard(props: PresetCardProps) {
  const { t, preset, actions, run, busy, editing, connected, onEdit, onSave, onDelete, onRun } =
    props;
  const isEditing = editing !== null && editing.id === preset.id;
  const failures = run ? run.steps.filter((step) => !step.ok) : [];

  if (isEditing && editing) {
    return (
      <Tile level={4} id={preset.id} title={preset.name || preset.id}>
        <PresetForm t={t} preset={editing} actions={actions} onChange={onEdit} />
        <div className="savebar-actions">
          <button
            type="button"
            className="btn"
            onClick={() => onSave(editing)}
            disabled={busy !== null}
          >
            {t('presets_save')}
          </button>
          <button type="button" className="btn btn--sm" onClick={() => onEdit(null)}>
            {t('presets_cancel')}
          </button>
        </div>
      </Tile>
    );
  }

  return (
    <Tile level={4} id={preset.id} title={preset.name || preset.id}>
      <Detail label={triggerLine(preset)} detail={stepNames(t, preset)} />
      {run && <StateWord tone={run.ok ? 'ok' : 'warn'} label={runLabel(t, run)} />}
      {failures.map((step) => (
        <Detail
          key={step.index}
          label={t(`presets_step_${step.kind}` as Parameters<T>[0])}
          detail={step.error}
        />
      ))}
      <div className="setting-control">
        <button
          type="button"
          className="btn"
          onClick={() => onRun(preset.id)}
          disabled={!connected || busy !== null}
        >
          {busy === `run:${preset.id}` ? t('presets_running') : t('presets_run')}
        </button>
        <button
          type="button"
          className="btn btn--sm"
          onClick={() => onEdit(preset)}
          disabled={!connected}
        >
          {t('presets_edit')}
        </button>
        <button
          type="button"
          className="btn btn--sm"
          onClick={() => onDelete(preset.id)}
          disabled={!connected || busy !== null}
        >
          {t('presets_delete')}
        </button>
      </div>
    </Tile>
  );
}
