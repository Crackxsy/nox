/**
 * "Lokale KI": measure this machine, recommend a model, download it through Ollama and check it
 * before it is saved — the same flow as `nox ai setup`, started with one click.
 *
 * The tile asks the core for a dry run when it first connects (cheap and read-only), so the
 * recommendation is on screen before anything is downloaded. A started run continues in the core;
 * the tile polls `ai.setup.status` once a second while it runs and stops polling when it ends.
 * Every refusal is shown in its own words: a machine that should not run a model, a missing or
 * stopped Ollama, a privacy mode that forbids downloads, a model that failed the probe.
 */

import { useCallback, useEffect, useState } from 'react';

import { errorText } from '../../../../shared/errors';
import { useRefreshOnConnect } from '../../hooks';
import { type Key, type T, fill, privacyLabel } from '../../i18n';
import { type IpcClient, api } from '../../ipc';
import {
  IDLE_LOCAL_AI,
  type LocalAiStatus,
  canStartSetup,
  downloadFraction,
  parseLocalAiStatus,
} from '../../model';
import { Tile } from '../../ui';

/** How often the tile asks for progress while a run is going. */
export const LOCAL_AI_POLL_MS = 1000;

const REASON_KEYS: Record<string, Key> = {
  vram: 'localai_reason_vram',
  ram_no_vram_reading: 'localai_reason_ram_no_vram_reading',
  ram_vram_too_small: 'localai_reason_ram_vram_too_small',
  no_llm_pi: 'localai_reason_no_llm_pi',
  no_llm_arm_low_ram: 'localai_reason_no_llm_arm_low_ram',
  no_llm_low_ram: 'localai_reason_no_llm_low_ram',
};

const PHASE_KEYS: Record<string, Key> = {
  detect: 'localai_phase_detect',
  ollama: 'localai_phase_ollama',
  install: 'localai_phase_install',
  pull: 'localai_phase_pull',
  probe: 'localai_phase_probe',
  write: 'localai_phase_write',
};

/** Outcomes worth a sentence; `recommended` (a finished check) and `no_llm` speak through the
 *  recommendation itself. */
const OUTCOME_KEYS: Record<string, Key> = {
  done: 'localai_out_done',
  ollama_missing: 'localai_out_ollama_missing',
  ollama_not_running: 'localai_out_ollama_not_running',
  install_failed: 'localai_out_install_failed',
  privacy_blocked: 'localai_out_privacy_blocked',
  declined: 'localai_out_declined',
  pull_failed: 'localai_out_pull_failed',
  probe_failed: 'localai_out_probe_failed',
  config_failed: 'localai_out_config_failed',
  failed: 'localai_out_failed',
};

const OK_OUTCOMES = new Set(['done']);

function gb(value: number, lang: string): string {
  return value.toLocaleString(lang, { maximumFractionDigits: 1 });
}

export interface LocalAiTileProps {
  t: T;
  lang: string;
  client: IpcClient | null;
}

export function LocalAiTile({ t, lang, client }: LocalAiTileProps) {
  const [status, setStatus] = useState<LocalAiStatus | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    async (c: IpcClient, cancelled: () => boolean = () => false) => {
      try {
        let parsed = parseLocalAiStatus(await api.localAiStatus(c));
        // Nothing measured yet in this core's life: a dry run fills the tile without changing
        // anything. A finished run is kept, so its outcome stays readable after a reload.
        if (!parsed.running && parsed.outcome === null) {
          parsed = parseLocalAiStatus(await api.localAiCheck(c));
        }
        if (cancelled()) return;
        setStatus(parsed);
        setError('');
      } catch (e) {
        if (cancelled()) return;
        setError(errorText(t('localai_unavailable'), e));
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
        .localAiStatus(client)
        .then((reply) => {
          if (!cancelled) setStatus(parseLocalAiStatus(reply));
        })
        .catch((e: unknown) => {
          if (!cancelled) setError(errorText(t('localai_unavailable'), e));
        });
    }, LOCAL_AI_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [client, running, t]);

  const act = async (fn: (c: IpcClient) => Promise<unknown>) => {
    if (!client || busy) return;
    setBusy(true);
    setError('');
    try {
      setStatus(parseLocalAiStatus(await fn(client)));
    } catch (e) {
      setError(errorText(t('error_prefix'), e));
    } finally {
      setBusy(false);
    }
  };

  const current = status ?? IDLE_LOCAL_AI;
  const hw = current.hardware;
  const rec = current.recommendation;
  const ollama = current.ollama;
  const fraction = downloadFraction(current.progress);
  const outcomeKey = current.outcome ? OUTCOME_KEYS[current.outcome] : undefined;
  const reasonKey = rec ? REASON_KEYS[rec.reason] : undefined;
  const phaseKey = running ? PHASE_KEYS[current.phase] : undefined;
  const disabled = !client || busy || running;

  return (
    <Tile id="localai" title={t('localai_title')} lede={t('localai_hint')}>
      {status === null && !error && <p className="muted">{t('localai_loading')}</p>}

      {hw && (
        <dl className="facts">
          <dt>{t('localai_ram')}</dt>
          <dd>
            {hw.ramGb === null ? t('localai_unknown') : fill(t('localai_gb'), gb(hw.ramGb, lang))}
          </dd>
          <dt>{t('localai_vram')}</dt>
          <dd>
            {hw.vramGb === null
              ? t('localai_vram_none')
              : fill(t('localai_gb'), gb(hw.vramGb, lang))}
          </dd>
          {hw.piGeneration !== null && (
            <>
              <dt>{t('localai_device')}</dt>
              <dd>{fill(t('localai_device_pi'), hw.piGeneration)}</dd>
            </>
          )}
          {rec?.model && (
            <>
              <dt>{t('localai_model')}</dt>
              <dd className="break">
                {fill(t('localai_model_size'), rec.model, gb(rec.downloadGb ?? 0, lang))}
              </dd>
            </>
          )}
          {rec?.model && ollama && (
            <>
              <dt>{t('localai_ollama')}</dt>
              <dd>
                {ollama.reachable
                  ? fill(t('localai_ollama_ok'), ollama.version || '?')
                  : ollama.installed
                    ? t('localai_ollama_stopped')
                    : t('localai_ollama_missing')}
              </dd>
            </>
          )}
        </dl>
      )}

      {reasonKey && <p className="hint">{t(reasonKey)}</p>}
      {rec?.model && !rec.measured && <p className="hint">{t('localai_unmeasured')}</p>}
      {rec?.model && ollama?.hasModel && <p className="hint">{t('localai_has_model')}</p>}
      {rec?.model && ollama && !ollama.reachable && ollama.installed && (
        <p className="hint">{t('localai_start_ollama')}</p>
      )}
      {rec?.model && ollama && !ollama.installed && ollama.instructions.length > 0 && (
        <>
          <p className="hint">{t('localai_install_how')}</p>
          <ul className="list">
            {ollama.instructions.map((line) => (
              <li key={line}>
                <code className="break">{line}</code>
              </li>
            ))}
          </ul>
        </>
      )}
      {rec?.model && current.pullBlocked && !ollama?.hasModel && (
        <p className="hint">
          {fill(t('localai_pull_blocked'), privacyLabel(t, current.privacyMode))}
        </p>
      )}

      {phaseKey && (
        <div role="status" aria-live="polite">
          <p>{fill(t(phaseKey), rec?.model ?? '')}</p>
          {current.phase === 'pull' && (
            <>
              <progress
                className="localai-progress"
                aria-label={t('localai_progress_label')}
                max={1}
                value={fraction ?? undefined}
              />
              <p className="muted">
                {fraction === null
                  ? current.progress?.status
                  : fill(
                      t('localai_progress'),
                      Math.round(fraction * 100),
                      gb((current.progress?.total ?? 0) / 1e9, lang),
                    )}
              </p>
            </>
          )}
        </div>
      )}

      {!running && outcomeKey && (
        <p
          role={OK_OUTCOMES.has(current.outcome ?? '') ? 'status' : 'alert'}
          className={OK_OUTCOMES.has(current.outcome ?? '') ? 'hint' : 'alert'}
        >
          {fill(t(outcomeKey), current.modelWritten || rec?.model || '', current.detail || '–')}
        </p>
      )}
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      {rec?.model && (
        <div className="tile-actions">
          {ollama?.canInstall ? (
            <button
              type="button"
              className="btn"
              disabled={disabled || current.pullBlocked}
              onClick={() => void act((c) => api.localAiStart(c, true))}
            >
              {t('localai_install_setup')}
            </button>
          ) : (
            <button
              type="button"
              className="btn"
              disabled={disabled || !canStartSetup(current)}
              onClick={() => void act((c) => api.localAiStart(c, false))}
            >
              {t('localai_setup')}
            </button>
          )}
          <button
            type="button"
            className="link"
            disabled={disabled}
            onClick={() => void act((c) => api.localAiCheck(c))}
          >
            {t('localai_recheck')}
          </button>
        </div>
      )}
    </Tile>
  );
}
