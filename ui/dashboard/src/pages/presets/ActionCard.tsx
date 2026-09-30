/**
 * One registered program: its path, a Test button, and what the last test did.
 *
 * Testing matters more here than anywhere else on the page. A program entry is the one thing on
 * this screen that can be perfectly well-formed and still wrong - a path that moved, a script
 * that needs its own folder - and the only way to find that out is to start it.
 */

import { type T } from '../../i18n';
import { type ActionTestResult, type PresetAction } from '../../model';
import { Detail, StateWord, Tile } from '../../ui';
import { ActionForm } from './ActionForm';

export interface ActionCardProps {
  t: T;
  action: PresetAction;
  test: ActionTestResult | undefined;
  busy: string | null;
  editing: PresetAction | null;
  connected: boolean;
  onEdit: (action: PresetAction | null) => void;
  onSave: (action: PresetAction) => void;
  onDelete: (id: string) => void;
  onTest: (id: string) => void;
}

export function ActionCard(props: ActionCardProps) {
  const { t, action, test, busy, editing, connected, onEdit, onSave, onDelete, onTest } = props;
  const isEditing = editing !== null && editing.id === action.id;

  if (isEditing && editing) {
    return (
      <Tile level={4} id={`action-${action.id}`} title={action.name || action.id}>
        <ActionForm t={t} action={editing} onChange={onEdit} />
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
    <Tile level={4} id={`action-${action.id}`} title={action.name || action.id}>
      <Detail label={action.command[0] ?? ''} detail={action.command.slice(1).join(' ')} />
      {test && (
        <StateWord
          tone={test.ok ? 'ok' : 'danger'}
          label={test.ok ? t('presets_action_ok') : t('presets_action_failed')}
        />
      )}
      {test && !test.ok && <Detail label={test.error} />}
      <div className="setting-control">
        <button
          type="button"
          className="btn"
          onClick={() => onTest(action.id)}
          disabled={!connected || busy !== null}
        >
          {busy === `test:${action.id}` ? t('presets_action_testing') : t('presets_action_test')}
        </button>
        <button
          type="button"
          className="btn btn--sm"
          onClick={() => onEdit(action)}
          disabled={!connected}
        >
          {t('presets_edit')}
        </button>
        <button
          type="button"
          className="btn btn--sm"
          onClick={() => onDelete(action.id)}
          disabled={!connected || busy !== null}
        >
          {t('presets_delete')}
        </button>
      </div>
    </Tile>
  );
}
