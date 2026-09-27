/**
 * Presets: one sentence, several systems.
 *
 * The page holds the data and the two editors hold the forms. Saving goes through `config.set`,
 * the same path the Settings page uses, so the core validates the whole section before anything
 * is written - a preset pointing at a program that was just deleted fails here, loudly, with the
 * reason, instead of being stored in a shape that would quietly do nothing when the phrase is
 * said. After every write the list is read back, so what the page shows is what the core kept.
 */

import { useCallback, useState } from 'react';

import { useIpcAction, useRefreshOnConnect } from '../hooks';
import { type T } from '../i18n';
import { type IpcClient, api } from '../ipc';
import {
  type ActionTestResult,
  type Preset,
  type PresetAction,
  type PresetRunResult,
  type PresetsPayload,
  blankAction,
  blankPreset,
  parseActionTest,
  parsePresets,
  parseRunResult,
  slugify,
} from '../model';
import { Hero, Tile } from '../ui';
import { ActionCard } from './presets/ActionCard';
import { ActionForm } from './presets/ActionForm';
import { PresetCard } from './presets/PresetCard';
import { PresetForm } from './presets/PresetForm';

export interface PresetsPageProps {
  t: T;
  client: IpcClient | null;
}

const EMPTY: PresetsPayload = { enabled: true, presets: [], actions: [] };

export function PresetsPage({ t, client }: PresetsPageProps) {
  const [data, setData] = useState<PresetsPayload>(EMPTY);
  const [editing, setEditing] = useState<Preset | null>(null);
  const [editingAction, setEditingAction] = useState<PresetAction | null>(null);
  const [runs, setRuns] = useState<Record<string, PresetRunResult>>({});
  const [tests, setTests] = useState<Record<string, ActionTestResult>>({});
  const [saveError, setSaveError] = useState('');
  const action = useIpcAction(client, t);

  const load = useCallback(async (c: IpcClient, cancelled: () => boolean) => {
    const payload = await api.presetsList(c);
    if (!cancelled()) setData(parsePresets(payload));
  }, []);

  useRefreshOnConnect(client, load);

  /** Write one list back through the settings path, then re-read what the core accepted. */
  const persist = async (path: string, value: unknown) => {
    if (!client) return;
    setSaveError('');
    const response = await api.configSet(client, { [path]: value });
    const errors = (response as { errors?: Record<string, string> }).errors ?? {};
    const reason = errors[path];
    if (reason) {
      setSaveError(reason);
      return;
    }
    const listing = await api.presetsList(client);
    setData(parsePresets(listing));
    setEditing(null);
    setEditingAction(null);
  };

  const savePreset = (preset: Preset) => {
    const id = preset.id || slugify(preset.name);
    const others = data.presets.filter((entry) => entry.id !== id);
    void action.run(`save:${id}`, () => persist('presets.items', [...others, { ...preset, id }]));
  };

  const deletePreset = (id: string) => {
    const remaining = data.presets.filter((entry) => entry.id !== id);
    void action.run(`delete:${id}`, () => persist('presets.items', remaining));
  };

  const saveAction = (entry: PresetAction) => {
    const id = entry.id || slugify(entry.name);
    const others = data.actions.filter((other) => other.id !== id);
    void action.run(`save-action:${id}`, () =>
      persist('presets.actions', [...others, { ...entry, id }]),
    );
  };

  const deleteAction = (id: string) => {
    const remaining = data.actions.filter((entry) => entry.id !== id);
    void action.run(`delete-action:${id}`, () => persist('presets.actions', remaining));
  };

  const runPreset = (id: string) => {
    void action.run(`run:${id}`, async (c) => {
      const response = await api.presetsActivate(c, id);
      const result = parseRunResult(response);
      if (result) setRuns((previous) => ({ ...previous, [id]: result }));
    });
  };

  const testAction = (id: string) => {
    void action.run(`test:${id}`, async (c) => {
      const response = await api.presetsTestAction(c, id);
      setTests((previous) => ({ ...previous, [id]: parseActionTest(response) }));
    });
  };

  const addingPreset = editing !== null && !data.presets.some((entry) => entry.id === editing.id);
  const addingAction =
    editingAction !== null && !data.actions.some((entry) => entry.id === editingAction.id);

  return (
    <>
      <Hero title={t('presets_title')} sub={t('presets_lede')} />

      {saveError && <p className="alert">{`${t('presets_save_failed')}: ${saveError}`}</p>}
      {action.error && <p className="alert">{action.error}</p>}

      <section aria-labelledby="h-presets">
        <h3 id="h-presets" className="rail-title">
          {t('presets_title')}
        </h3>

        {data.presets.length === 0 && !addingPreset && (
          <Tile level={4} title={t('presets_none')}>
            <p className="muted">{t('presets_none_hint')}</p>
          </Tile>
        )}

        {data.presets.map((preset) => (
          <PresetCard
            key={preset.id}
            t={t}
            preset={preset}
            actions={data.actions}
            run={runs[preset.id]}
            busy={action.busy}
            editing={editing}
            connected={client !== null}
            onEdit={setEditing}
            onSave={savePreset}
            onDelete={deletePreset}
            onRun={runPreset}
          />
        ))}

        {addingPreset && editing && (
          <Tile level={4} title={editing.name || t('presets_add')}>
            <PresetForm t={t} preset={editing} actions={data.actions} onChange={setEditing} />
            <div className="savebar-actions">
              <button
                type="button"
                className="btn"
                onClick={() => savePreset(editing)}
                disabled={action.busy !== null || editing.name.trim() === ''}
              >
                {t('presets_save')}
              </button>
              <button type="button" className="btn btn--sm" onClick={() => setEditing(null)}>
                {t('presets_cancel')}
              </button>
            </div>
          </Tile>
        )}

        {editing === null && (
          <button
            type="button"
            className="btn"
            onClick={() => setEditing(blankPreset(''))}
            disabled={client === null}
          >
            {t('presets_add')}
          </button>
        )}
      </section>

      <section aria-labelledby="h-preset-actions">
        <h3 id="h-preset-actions" className="rail-title">
          {t('presets_actions_title')}
        </h3>
        <p className="muted">{t('presets_actions_lede')}</p>

        {data.actions.length === 0 && !addingAction && (
          <Tile level={4} title={t('presets_actions_none')} />
        )}

        {data.actions.map((entry) => (
          <ActionCard
            key={entry.id}
            t={t}
            action={entry}
            test={tests[entry.id]}
            busy={action.busy}
            editing={editingAction}
            connected={client !== null}
            onEdit={setEditingAction}
            onSave={saveAction}
            onDelete={deleteAction}
            onTest={testAction}
          />
        ))}

        {addingAction && editingAction && (
          <Tile level={4} title={editingAction.name || t('presets_action_add')}>
            <ActionForm t={t} action={editingAction} onChange={setEditingAction} />
            <div className="savebar-actions">
              <button
                type="button"
                className="btn"
                onClick={() => saveAction(editingAction)}
                disabled={action.busy !== null || editingAction.name.trim() === ''}
              >
                {t('presets_save')}
              </button>
              <button type="button" className="btn btn--sm" onClick={() => setEditingAction(null)}>
                {t('presets_cancel')}
              </button>
            </div>
          </Tile>
        )}

        {editingAction === null && (
          <button
            type="button"
            className="btn"
            onClick={() => setEditingAction(blankAction(''))}
            disabled={client === null}
          >
            {t('presets_action_add')}
          </button>
        )}
      </section>
    </>
  );
}
