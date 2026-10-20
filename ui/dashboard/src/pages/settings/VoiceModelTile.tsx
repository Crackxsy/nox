/**
 * "Stimme": the default voice's model files (Supertonic 3, about 400 MB) — what is there, what is
 * missing, the license that comes with them, and a download started with one click.
 *
 * The click is the consent; the core then runs the download as its own process and the tile polls
 * `voice.model.status` once a second while it runs. Every outcome is said as it is: downloaded
 * (used from Nox's next start, because the voice worker loads its engine once), already there,
 * refused by the privacy mode, or failed with the reason the download gave.
 */

import { useCallback, useEffect, useState } from 'react';

import { errorText } from '../../../../shared/errors';
import { useRefreshOnConnect } from '../../hooks';
import { type Key, type T, fill, optionLabel, privacyLabel } from '../../i18n';
import { type IpcClient, api } from '../../ipc';
import {
  IDLE_VOICE_MODEL,
  type VoiceModelStatus,
  canDownloadVoiceModel,
  parseVoiceModelStatus,
  voiceModelFraction,
} from '../../model';
import { Tile } from '../../ui';

/** How often the tile asks for progress while a download is running. */
export const VOICE_MODEL_POLL_MS = 1000;

const OUTCOME_KEYS: Record<string, Key> = {
  done: 'voicemodel_out_done',
  already_present: 'voicemodel_out_already_present',
  privacy_blocked: 'voicemodel_out_privacy_blocked',
  failed: 'voicemodel_out_failed',
};

const OK_OUTCOMES = new Set(['done', 'already_present']);

function mb(bytes: number, lang: string): string {
  return Math.round(bytes / 1e6).toLocaleString(lang);
}

export interface VoiceModelTileProps {
  t: T;
  lang: string;
  client: IpcClient | null;
}

export function VoiceModelTile({ t, lang, client }: VoiceModelTileProps) {
  const [status, setStatus] = useState<VoiceModelStatus | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    async (c: IpcClient, cancelled: () => boolean = () => false) => {
      try {
        const parsed = parseVoiceModelStatus(await api.voiceModelStatus(c));
        if (cancelled()) return;
        setStatus(parsed);
        setError('');
      } catch (e) {
        if (cancelled()) return;
        setError(errorText(t('voicemodel_unavailable'), e));
      }
    },
    [t],
  );
  useRefreshOnConnect(client, load);

  const running = status?.running ?? false;
  useEffect(() => {
    if (!client || !running) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void api
        .voiceModelStatus(client)
        .then((reply) => {
          if (!cancelled) setStatus(parseVoiceModelStatus(reply));
        })
        .catch((e: unknown) => {
          if (!cancelled) setError(errorText(t('voicemodel_unavailable'), e));
        });
    }, VOICE_MODEL_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [client, running, t]);

  const download = async () => {
    if (!client || busy) return;
    setBusy(true);
    setError('');
    try {
      setStatus(parseVoiceModelStatus(await api.voiceModelDownload(client)));
    } catch (e) {
      setError(errorText(t('error_prefix'), e));
    } finally {
      setBusy(false);
    }
  };

  const current = status ?? IDLE_VOICE_MODEL;
  const fraction = voiceModelFraction(current.progress);
  const outcomeKey = current.outcome ? OUTCOME_KEYS[current.outcome] : undefined;
  const ok = OK_OUTCOMES.has(current.outcome ?? '');
  const otherEngine =
    current.configuredEngine !== '' && current.configuredEngine !== current.engine;

  return (
    <Tile id="voicemodel" title={t('voicemodel_title')} lede={t('voicemodel_hint')}>
      {status === null && !error && <p className="muted">{t('voicemodel_loading')}</p>}

      {status && current.files > 0 && (
        <dl className="facts">
          <dt>{t('voicemodel_engine')}</dt>
          <dd>{optionLabel(t, 'voice.tts.engine', current.configuredEngine)}</dd>
          <dt>{t('voicemodel_files')}</dt>
          <dd>
            {current.present
              ? fill(t('voicemodel_files_present'), current.files)
              : fill(t('voicemodel_files_missing'), current.missing.length, current.files)}
          </dd>
          {!current.present && (
            <>
              <dt>{t('voicemodel_size')}</dt>
              <dd>{fill(t('voicemodel_size_value'), mb(current.missingBytes, lang))}</dd>
            </>
          )}
          <dt>{t('voicemodel_folder')}</dt>
          <dd className="break">
            <code>{current.directory}</code>
          </dd>
          {current.license && (
            <>
              <dt>{t('voicemodel_license')}</dt>
              <dd>{current.license}</dd>
            </>
          )}
        </dl>
      )}

      {current.license === 'OpenRAIL-M' && (
        <p className="hint">{t('voicemodel_license_openrail')}</p>
      )}
      {otherEngine && (
        <p className="hint">
          {fill(
            t('voicemodel_other_engine'),
            optionLabel(t, 'voice.tts.engine', current.configuredEngine),
          )}
        </p>
      )}
      {!current.present && current.downloadBlocked && (
        <p className="hint">
          {fill(t('voicemodel_blocked'), privacyLabel(t, current.privacyMode))}
        </p>
      )}

      {running && (
        <div role="status" aria-live="polite">
          <p>{t('voicemodel_running')}</p>
          <progress
            className="localai-progress"
            aria-label={t('voicemodel_progress_label')}
            max={1}
            value={fraction ?? undefined}
          />
          {fraction !== null && current.progress && (
            <p className="muted">
              {fill(
                t('voicemodel_progress'),
                Math.round(fraction * 100),
                mb(current.progress.total, lang),
                current.progress.index,
                current.progress.count,
              )}
            </p>
          )}
        </div>
      )}

      {!running && outcomeKey && (
        <p role={ok ? 'status' : 'alert'} className={ok ? 'hint' : 'alert'}>
          {fill(t(outcomeKey), current.detail || '–')}
        </p>
      )}
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      {status && !current.present && current.files > 0 && (
        <div className="tile-actions">
          <button
            type="button"
            className="btn"
            disabled={!client || busy || !canDownloadVoiceModel(current)}
            onClick={() => void download()}
          >
            {t('voicemodel_download')}
          </button>
        </div>
      )}
    </Tile>
  );
}
