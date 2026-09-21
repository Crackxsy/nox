/**
 * Clips page (ST-15-05, Spec v0.6 Clip Pipeline): the review queue as a horizontal rail of tiles —
 * one tile per clip, with its tag field and the two actions. No upload button anywhere in this page
 * by design (spec §10): `clip.export` only ever copies into the configured `export_root` on local
 * disk, never calls a network API.
 *
 * Refusals are a designed state, not an error banner. On a shipped install the `dashboard` role is
 * not on the core's allow-list for `clip.list`, so the *first* thing a new user saw on this tab was
 * `Fehler: role 'dashboard' may not call 'clip.list'`. The page now says what that means and what
 * is and is not affected, and keeps the core's own sentence in a muted second line.
 */

import { useState } from 'react';

import { failureKind, reasonText } from '../../../shared/errors';
import { formatDuration } from '../../../shared/format';
import { useIpcAction, useRefreshOnConnect } from '../hooks';
import { type Lang, type T, clipSourceLabel, clipStatusLabel, clipTriggerLabel } from '../i18n';
import { type IpcClient, api } from '../ipc';
import {
  type ClipRecord,
  type DashboardState,
  applyClipListResult,
  applyClipTagResult,
  parseClipExportResult,
} from '../model';
import { Detail, Hero, Rail, StateWord, Tile, type Tone } from '../ui';

export interface ClipsPageProps {
  t: T;
  lang: Lang;
  state: DashboardState;
  /** null while offline: every request would fail, so the controls are disabled instead. */
  client: IpcClient | null;
  onState: (updater: (s: DashboardState) => DashboardState) => void;
}

const STATUS_TONE: Record<string, Tone> = {
  new: 'warn',
  reviewed: 'ok',
  exported: 'ok',
  discarded: 'off',
};

/** How the page explains itself when `clip.list` did not return a list. */
interface Blocked {
  title: string;
  body: string;
  detail: string | null;
}

export function ClipsPage({ t, lang, state, client, onState }: ClipsPageProps) {
  const [loading, setLoading] = useState(false);
  const [blocked, setBlocked] = useState<Blocked | null>(null);
  const [editing, setEditing] = useState<Record<string, string>>({});
  const [exportMsg, setExportMsg] = useState<Record<string, string>>({});
  const { busy, error, setError, run } = useIpcAction(client, t);
  const disabled = client === null;

  const load = async (c: IpcClient, cancelled: () => boolean = () => false) => {
    setLoading(true);
    setError('');
    try {
      const payload = await api.clipList(c, null, 50);
      if (cancelled()) return;
      setBlocked(null);
      onState((s) => applyClipListResult(s, payload));
    } catch (e) {
      if (cancelled()) return;
      const refused = failureKind(e) === 'refused';
      setBlocked({
        title: refused ? t('clips_refused') : t('clips_unavailable'),
        body: refused ? t('clips_refused_hint') : t('clips_unavailable_hint'),
        detail: reasonText(e) || null,
      });
    } finally {
      if (!cancelled()) setLoading(false);
    }
  };

  useRefreshOnConnect(client, load);

  const tagsText = (clip: ClipRecord) => editing[clip.id] ?? clip.tags.join(', ');

  const saveTags = (clip: ClipRecord) =>
    run(clip.id, async (c) => {
      const tags = tagsText(clip)
        .split(',')
        .map((s) => s.trim())
        .filter((s) => s.length > 0);
      const result = await api.clipTag(c, clip.id, tags, null);
      onState((s) => applyClipTagResult(s, result));
      setEditing((e) => {
        const next = { ...e };
        delete next[clip.id];
        return next;
      });
    });

  const exportClip = (clip: ClipRecord) =>
    run(clip.id, async (c) => {
      setExportMsg((m) => ({ ...m, [clip.id]: '' }));
      const result = parseClipExportResult(await api.clipExport(c, clip.id));
      // The authoritative status flip arrives as `clip.exported` on the live subscription; this is
      // only the immediate confirmation for the button the user just pressed. `export_path` is
      // optional in the contract, so it is never interpolated into the sentence.
      const message =
        result?.ok === true
          ? t('clips_export_done')
          : result?.reason
            ? `${t('clips_export_failed')}: ${result.reason}`
            : t('clips_export_failed');
      setExportMsg((m) => ({ ...m, [clip.id]: message }));
    });

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

      {blocked ? (
        <div className="tiles tiles--single">
          <Tile id="clips-blocked" title={blocked.title} lede={blocked.body}>
            {blocked.detail && <p className="hint break detail-sub">{blocked.detail}</p>}
          </Tile>
        </div>
      ) : clips.length === 0 ? (
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
              level={4}
              eyebrow={clip.source ? clipSourceLabel(t, clip.source) : undefined}
              title={clip.triggerKind ? clipTriggerLabel(t, clip.triggerKind) : t('unknown')}
              lede={clip.durationS ? formatDuration(clip.durationS, lang) : undefined}
            >
              <StateWord
                tone={STATUS_TONE[clip.status] ?? 'off'}
                label={clipStatusLabel(t, clip.status)}
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
                  <Detail label={exportMsg[clip.id]} detail={clip.filePath || null} />
                </p>
              )}
            </Tile>
          ))}
        </Rail>
      )}
    </div>
  );
}
