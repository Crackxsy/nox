/**
 * The personality text — the character Nox puts in front of every conversation.
 *
 * The draft is owned here and marked dirty explicitly, so a reload triggered by an unrelated
 * `settings.changed` event cannot overwrite what somebody is typing. (The previous check compared
 * against a value captured on the first render and therefore always overwrote.)
 *
 * The file path is not printed: it is a machine path, and §7 says a label is a word. The tile says
 * where the text is kept in a sentence instead.
 */

import { useEffect, useRef, useState } from 'react';

import { errorText } from '../../../../shared/errors';
import type { T } from '../../i18n';
import { type IpcClient, api } from '../../ipc';
import type { Personality } from '../../model';
import { Tile } from '../../ui';

export interface PersonalityTileProps {
  t: T;
  client: IpcClient | null;
  personality: Personality | null;
  failed: boolean;
  onSaved: (next: Personality) => void;
}

export function PersonalityTile({ t, client, personality, failed, onSaved }: PersonalityTileProps) {
  const [draft, setDraft] = useState('');
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  // Adopt what the core reports, unless the user has unsaved edits in the box.
  useEffect(() => {
    if (personality && !dirtyRef.current) setDraft(personality.text);
  }, [personality]);

  const disabled = client === null;

  const save = async () => {
    if (!client || saving) return;
    setSaving(true);
    setError('');
    setSaved(false);
    try {
      await api.personalitySet(client, draft);
      onSaved({ text: draft, path: personality?.path ?? '' });
      setDirty(false);
      setSaved(true);
    } catch (e) {
      setError(errorText(t('error_prefix'), e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Tile
      id="personality"
      title={t('personality_title')}
      lede={t('personality_hint')}
    >
      {failed ? (
        <p className="muted">{t('personality_unavailable')}</p>
      ) : (
        <>
          {error && (
            <p role="alert" className="alert">
              {error}
            </p>
          )}
          <div className="field">
            <label htmlFor="personality-text" className="label">
              {t('personality_label')}
            </label>
            <textarea
              id="personality-text"
              className="textarea"
              rows={12}
              disabled={disabled}
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                setDirty(true);
                setSaved(false);
              }}
            />
            <p className="hint">{t('personality_file')}</p>
          </div>
          <div className="tile-actions">
            <button
              type="button"
              className="btn"
              disabled={disabled || saving || draft === (personality?.text ?? '')}
              onClick={() => void save()}
            >
              {saving ? t('personality_saving') : t('personality_save')}
            </button>
            {saved && (
              <p role="status" className="ok-note">
                {t('personality_saved')}
              </p>
            )}
          </div>
        </>
      )}
    </Tile>
  );
}
