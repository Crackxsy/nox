/**
 * The one loader behind the Settings page.
 *
 * Three things it has to get right, all of which the inline version got wrong:
 *  - *cancellation*: five `Promise.allSettled` results land out of order, so a reload that started
 *    later can finish earlier. Every write is guarded by a token, and only the newest load writes.
 *  - *debouncing*: a save that touches ten paths produces ten `settings.changed` events. The
 *    revision is debounced, so that is one reload, not ten.
 *  - *not clobbering the user*: the personality textarea keeps whatever is in it while it is dirty.
 *    The old check compared against the first render's empty string and therefore always overwrote.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { type IpcClient, api } from '../../ipc';
import {
  type EditableConfig,
  type HealthHistoryEntry,
  type Personality,
  type SecretStatus,
  parseEditableConfig,
  parseHealthHistory,
  parsePersonality,
  parsePinConfigured,
  parseSecretStatus,
} from '../../model';

/** How long a burst of `settings.changed` events is collapsed into one reload. */
export const RELOAD_DEBOUNCE_MS = 250;

export interface SettingsData {
  config: EditableConfig | null;
  configFailed: boolean;
  secrets: SecretStatus[] | null;
  secretsFailed: boolean;
  personality: Personality | null;
  personalityFailed: boolean;
  history: HealthHistoryEntry[];
  pinConfigured: boolean;
  loading: boolean;
  /** Reload everything now (the "Aktualisieren" link). */
  reload: () => void;
  /** Re-read only the secret list, after a `secrets.set`/`secrets.delete`. */
  reloadSecrets: (c: IpcClient) => Promise<void>;
  /** Adopt the values a save applied, without a round trip. */
  applyValues: (values: Record<string, unknown>) => void;
  setPersonality: (next: Personality) => void;
}

export function useSettingsData(client: IpcClient | null, revision: number): SettingsData {
  const [config, setConfig] = useState<EditableConfig | null>(null);
  const [configFailed, setConfigFailed] = useState(false);
  const [secrets, setSecrets] = useState<SecretStatus[] | null>(null);
  const [secretsFailed, setSecretsFailed] = useState(false);
  const [personality, setPersonality] = useState<Personality | null>(null);
  const [personalityFailed, setPersonalityFailed] = useState(false);
  const [history, setHistory] = useState<HealthHistoryEntry[]>([]);
  const [pinConfigured, setPinConfigured] = useState(false);
  const [loading, setLoading] = useState(false);
  const [manual, setManual] = useState(0);

  /** Incremented per load; a result whose token is stale is discarded. */
  const tokenRef = useRef(0);

  const load = useCallback(async (c: IpcClient) => {
    const token = ++tokenRef.current;
    const current = () => tokenRef.current === token;
    setLoading(true);
    const [cfg, sec, pers, hist, pinState] = await Promise.allSettled([
      api.configGet(c),
      api.secretsStatus(c),
      api.personalityGet(c),
      api.healthHistory(c, 30),
      api.pinStatus(c),
    ]);
    if (!current()) return;

    const parsedConfig = cfg.status === 'fulfilled' ? parseEditableConfig(cfg.value) : null;
    setConfig(parsedConfig);
    setConfigFailed(parsedConfig === null);

    const parsedSecrets = sec.status === 'fulfilled' ? parseSecretStatus(sec.value) : null;
    setSecrets(parsedSecrets);
    setSecretsFailed(parsedSecrets === null);

    const parsedPersonality = pers.status === 'fulfilled' ? parsePersonality(pers.value) : null;
    setPersonality(parsedPersonality);
    setPersonalityFailed(parsedPersonality === null);

    if (hist.status === 'fulfilled') setHistory(parseHealthHistory(hist.value));

    // A refused or failed request means "no PIN known here": the page then sends none and the
    // core's own refusal is what tells the user a PIN is needed.
    setPinConfigured(pinState.status === 'fulfilled' && parsePinConfigured(pinState.value));
    setLoading(false);
  }, []);

  useEffect(() => {
    if (!client) return;
    const timer = setTimeout(() => void load(client), RELOAD_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      // Anything still in flight belongs to a superseded load.
      tokenRef.current += 1;
    };
  }, [client, revision, manual, load]);

  const reloadSecrets = useCallback(async (c: IpcClient) => {
    const parsed = parseSecretStatus(await api.secretsStatus(c));
    setSecrets(parsed);
    setSecretsFailed(parsed === null);
  }, []);

  const applyValues = useCallback((values: Record<string, unknown>) => {
    setConfig((c) => (c ? { ...c, values: { ...c.values, ...values } } : c));
  }, []);

  return {
    config,
    configFailed,
    secrets,
    secretsFailed,
    personality,
    personalityFailed,
    history,
    pinConfigured,
    loading,
    reload: () => setManual((n) => n + 1),
    reloadSecrets,
    applyValues,
    setPersonality: (next: Personality) => setPersonality(next),
  };
}
