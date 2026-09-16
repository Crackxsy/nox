/**
 * Clips page (ST-15-05, Spec v0.6 Clip Pipeline): the review queue as a horizontal rail of tiles —
 * one tile per clip, with its tag field and the two actions. No upload button anywhere in this page
 * by design (spec §10): `clip.export` only ever copies into the configured `export_root` on local
 * disk, never calls a network API.
 */

import { useEffect, useState } from 'react';

import { type Key, type T } from '../i18n';
import { type IpcClient, api } from '../ipc';
import { type ClipRecord, type DashboardState, applyClipList } from '../model';
import { Hero, Rail, StateWord, Tile } from '../ui';

export interface ClipsPageProps {
  t: T;
  state: DashboardState;
  /** null while offline: every request would fail, so the controls are disabled instead. */
  client: IpcClient | null;
  onState: (updater: (s: DashboardState) => DashboardState) => void;
}

const STATUS_KEYS: Record<string, Key> = {
  new: 'clips_status_new',
  reviewed: 'clips_status_reviewed',
  exported: 'clips_status_exported',
  discarded: 'clips_status_discarded',
};

const STATUS_TONE: Record<string, string> = {
  new: 'limited',
  exported: 'available',
  discarded: 'off',
};

export function ClipsPage({ t, state, client, onState }: ClipsPageProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [exportMsg, setExportMsg] = useState<Record<string, string>>({});
  const disabled = client === null;

  const load = async (c: IpcClient) => {
    setLoading(true);
    setError('');
    try {
      const payload = await api.clipList(c, null, 50);
      onState((s) => applyClipList(s, payload));
    } catch (e) {
      setError(`${t('error_prefix')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (client) void load(client);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run once per (re)connect
  }, [client]);

  const tagsText = (clip: ClipRecord) => editing[clip.id] ?? clip.tags.join(', ');

  const saveTags = async (clip: ClipRecord) => {
    if (!client) return;
    setBusy(clip.id);
    setError('');
    try {
      const tags = tagsText(clip)
        .split(',')
        .map((s) => s.trim())
        .filter((s) => s.length > 0);
      const result = (await api.clipTag(client, clip.id, tags, null)) as { clip?: unknown };
      if (result.clip) onState((s) => applyClipList(s, { clips: [result.clip] }));
      setEditing((e) => {
        const next = { ...e };
        delete next[clip.id];
        return next;
      });
    } catch (e) {
      setError(`${t('error_prefix')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const exportClip = async (clip: ClipRecord) => {
    if (!client) return;
    setBusy(clip.id);
    setError('');
    setExportMsg((m) => ({ ...m, [clip.id]: '' }));
    try {
      const result = (await api.clipExport(client, clip.id)) as {
        ok: boolean;
        export_path: string | null;
        reason: string;
      };
      setExportMsg((m) => ({
        ...m,
        [clip.id]: result.ok
          ? `${t('clips_export_done')}: ${result.export_path}`
          : `${t('clips_export_failed')}: ${result.reason}`,
      }));
      // The authoritative status flip arrives as `clip.exported` on the live subscription; this
      // is only the immediate confirmation for the button the user just pressed.
    } catch (e) {
      setError(`${t('error_prefix')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const clips = state.clips.items;

  return (
    <div className="page wrap">
      <Hero
        title={t('hero_clips')}
        sub={t('hero_clips_sub')}
        links={
          <button
            type="button"
            className="link"
            disabled={disabled || loading}
            onClick={() => client && void load(client)}
          >
            {loading ? t('clips_loading') : t('clips_refresh')}
          </button>
        }
      />

      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      {clips.length === 0 ? (
        <section aria-labelledby="h-clips">
          <div className="rail-head">
            <div>
              <h3 id="h-clips" className="rail-title">
                {t('clips_title')}
              </h3>
              <p className="rail-sub">{t('clips_empty')}</p>
            </div>
          </div>
        </section>
      ) : (
        <Rail t={t} title={t('clips_title')} sub={t('clips_hint')}>
          {clips.map((clip) => (
            <Tile
              key={clip.id}
              eyebrow={clip.source || t('clips_col_source')}
              title={clip.triggerKind || t('clips_col_trigger')}
              lede={
                clip.durationS ? `${clip.durationS.toFixed(1)} s` : t('unknown')
              }
            >
              <StateWord
                status={STATUS_TONE[clip.status] ?? 'off'}
                label={t(STATUS_KEYS[clip.status] ?? 'clips_status_new')}
              />

              <div className="field field--spaced">
                <label htmlFor={`clip-tags-${clip.id}`} className="label">
                  {t('clips_tags_label')}
                  <span className="sr-only"> ({clip.id})</span>
                </label>
                <input
                  id={`clip-tags-${clip.id}`}
                  type="text"
                  className="input"
                  value={tagsText(clip)}
                  disabled={disabled || busy === clip.id}
                  onChange={(e) => setEditing((ed) => ({ ...ed, [clip.id]: e.target.value }))}
                />
              </div>

              <div className="tile-actions">
                <button
                  type="button"
                  className="btn btn--sm"
                  disabled={disabled || busy === clip.id || clip.status === 'exported'}
                  onClick={() => void exportClip(clip)}
                >
                  {t('clips_export_button')}
                </button>
                <button
                  type="button"
                  className="link"
                  disabled={disabled || busy === clip.id}
                  onClick={() => void saveTags(clip)}
                >
                  {t('clips_tags_save')}
                </button>
              </div>

              {exportMsg[clip.id] && (
                <p role="status" className="hint break">
                  {exportMsg[clip.id]}
                </p>
              )}
            </Tile>
          ))}
        </Rail>
      )}
    </div>
  );
}
