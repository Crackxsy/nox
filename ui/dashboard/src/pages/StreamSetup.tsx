/**
 * Stream page, "Set up": the two one-click actions that put Nox into the broadcast.
 *
 * - OBS overlay (`stream.overlay.setup`): a browser source in the current scene, authenticated by a
 *   persistent, read-only overlay credential - so it survives a restart of Nox, unlike a screen
 *   capture of the desktop pet. The credential is never shown here; only whether one exists.
 * - Own audio track (`stream.audio_track.setup`): Nox speaks into the virtual cable as well, and
 *   OBS records it as its own source, optionally on chosen tracks.
 *
 * Both change OBS, so the core asks the user in the shell's permission dialog first; the button
 * says so while the request is pending.
 */

import { useState } from 'react';

import { formatTimestamp } from '../../../shared/format';
import { useIpcAction, useRefreshOnConnect } from '../hooks';
import { type Lang, type T, fill } from '../i18n';
import { type IpcClient, api } from '../ipc';
import { StateWord, Tile } from '../ui';

interface OverlayStatus {
  available: boolean;
  configured: boolean;
  createdAt: string | null;
}

function parseOverlayStatus(raw: unknown): OverlayStatus {
  const obj = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>;
  const credential = obj.credential as Record<string, unknown> | null | undefined;
  return {
    available: obj.available !== false,
    configured: obj.configured === true,
    createdAt: typeof credential?.created_at === 'string' ? credential.created_at : null,
  };
}

function field(raw: unknown, key: string): string {
  if (typeof raw !== 'object' || raw === null) return '';
  const value = (raw as Record<string, unknown>)[key];
  return typeof value === 'string' ? value : '';
}

const TRACKS = [1, 2, 3, 4, 5, 6] as const;

export interface StreamSetupProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
}

export function StreamSetup({ t, lang, client }: StreamSetupProps) {
  const { busy, run } = useIpcAction(client, t);
  const [overlay, setOverlay] = useState<OverlayStatus>({
    available: true,
    configured: false,
    createdAt: null,
  });
  const [overlayNote, setOverlayNote] = useState<string | null>(null);
  const [audioNote, setAudioNote] = useState<string | null>(null);
  const [tracks, setTracks] = useState<number[]>([]);
  const disabled = client === null;

  useRefreshOnConnect(client, async (c, cancelled) => {
    try {
      const status = parseOverlayStatus(await api.overlayStatus(c));
      if (!cancelled()) setOverlay(status);
    } catch {
      /* keep the last known state */
    }
  });

  const refreshOverlay = async (c: IpcClient) => setOverlay(parseOverlayStatus(await api.overlayStatus(c)));

  const setupOverlay = () =>
    run('overlay', async (c) => {
      setOverlayNote(t('stream_setup_confirm'));
      const result = await api.overlaySetup(c);
      const ok = typeof result === 'object' && result !== null && (result as { ok?: unknown }).ok === true;
      setOverlayNote(
        ok
          ? fill(t('stream_overlay_done'), field(result, 'input_name'), field(result, 'scene'))
          : fill(t('stream_setup_failed'), field(result, 'error') || field(result, 'reason')),
      );
      await refreshOverlay(c);
    });

  const revokeOverlay = () =>
    run('overlay-revoke', async (c) => {
      await api.overlayRevoke(c);
      setOverlayNote(t('stream_overlay_revoked'));
      await refreshOverlay(c);
    });

  const setupAudio = () =>
    run('audio', async (c) => {
      setAudioNote(t('stream_setup_confirm'));
      const result = await api.audioTrackSetup(c, tracks);
      const ok = typeof result === 'object' && result !== null && (result as { ok?: unknown }).ok === true;
      setAudioNote(
        ok
          ? fill(t('stream_audio_done'), field(result, 'nox_plays_to'), field(result, 'obs_source'))
          : fill(t('stream_setup_failed'), field(result, 'reason')),
      );
    });

  const toggleTrack = (n: number) =>
    setTracks((prev) => (prev.includes(n) ? prev.filter((x) => x !== n) : [...prev, n].sort()));

  return (
    <section aria-labelledby="h-stream-setup">
      <div className="rail-head">
        <div>
          <h3 id="h-stream-setup" className="rail-title">
            {t('stream_setup_title')}
          </h3>
          <p className="rail-sub">{t('stream_setup_sub')}</p>
        </div>
      </div>
      <div className="tiles">
        <Tile level={4} id="obs-overlay" title={t('stream_overlay_title')} lede={t('stream_overlay_lede')}>
          <StateWord
            tone={overlay.configured ? 'ok' : 'off'}
            label={overlay.configured ? t('stream_overlay_on') : t('stream_overlay_off')}
          />
          {overlay.configured && overlay.createdAt && (
            <p className="hint">{fill(t('stream_overlay_since'), formatTimestamp(overlay.createdAt, lang))}</p>
          )}
          <div className="tile-actions">
            <button
              type="button"
              className="btn"
              disabled={disabled || !overlay.available || busy === 'overlay'}
              onClick={() => void setupOverlay()}
            >
              {t('stream_overlay_setup')}
            </button>
            {overlay.configured && (
              <button
                type="button"
                className="btn btn--quiet"
                disabled={disabled || busy === 'overlay-revoke'}
                onClick={() => void revokeOverlay()}
              >
                {t('stream_overlay_revoke')}
              </button>
            )}
          </div>
          {overlayNote && (
            <p role="status" className="hint break">
              {overlayNote}
            </p>
          )}
        </Tile>
        <Tile level={4} id="obs-audio" title={t('stream_audio_title')} lede={t('stream_audio_lede')}>
          <p id="obs-audio-tracks" className="hint">
            {t('stream_audio_tracks')}
          </p>
          <ul className="choice-list" aria-labelledby="obs-audio-tracks">
            {TRACKS.map((n) => (
              <li key={n} className="choice-row">
                <label className="choice-label">
                  <input
                    type="checkbox"
                    checked={tracks.includes(n)}
                    onChange={() => toggleTrack(n)}
                    disabled={disabled}
                  />
                  <span>{fill(t('stream_audio_track'), n)}</span>
                </label>
              </li>
            ))}
          </ul>
          <div className="tile-actions">
            <button
              type="button"
              className="btn"
              disabled={disabled || busy === 'audio'}
              onClick={() => void setupAudio()}
            >
              {t('stream_audio_setup')}
            </button>
          </div>
          {audioNote && (
            <p role="status" className="hint break">
              {audioNote}
            </p>
          )}
        </Tile>
      </div>
    </section>
  );
}
