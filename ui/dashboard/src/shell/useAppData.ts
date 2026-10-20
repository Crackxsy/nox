/**
 * The few lists the shell itself needs, read once per connection and again on demand:
 *
 *  - facts Nox noted from a phone message or pasted text and keeps out of its prompt until they are
 *    confirmed (`memory.profile.list`) - "Wartet auf dich" on Heute;
 *  - skills Nox finished learning that wait to be activated (`skills.list`, local only);
 *  - the presets ("Abläufe") for Heute and the command palette (`presets.list`);
 *  - the person's name and the editable configuration's shape, for the greeting and the first run
 *    (`config.get`).
 *
 * Every read is best effort: a list that could not be read is empty, never invented, and the page
 * that owns the full list (Einstellungen › Über dich, Fähigkeiten) still shows the core's error.
 */

import { useCallback, useEffect, useState } from 'react';

import { isRecord, str } from '../../../shared/guards';
import { type IpcClient, api } from '../ipc';
import { type Preset, parsePresets } from '../model/presets';
import { type ProfileFact, isNewlyLearned, parseProfileFacts } from '../model/profile';

/** How many learned skills wait to be activated (`skills.list`; the full parser lives with the page). */
export function readySkills(payload: unknown): number {
  if (!isRecord(payload) || !Array.isArray(payload.skills)) return 0;
  return payload.skills.filter((skill) => isRecord(skill) && skill.state === 'ready').length;
}

export interface AppData {
  /** Facts that wait for a yes (not used by Nox until confirmed). */
  unconfirmed: ProfileFact[];
  /** Facts Nox learned by itself in the last days, shown quietly. */
  fresh: ProfileFact[];
  /** Skills ready to be activated. */
  skillsReady: number;
  presets: Preset[];
  /** `identity.user_display_name`, '' when not set or not readable. */
  userName: string;
  /** Whether `config.get` answered at all (the first run is only offered then). */
  configLoaded: boolean;
  reload: () => void;
  /** Drops a fact from the waiting list at once, before the next reload confirms it. */
  dropFact: (id: number) => void;
}

export function useAppData(client: IpcClient | null, device: boolean, settingsRevision: number): AppData {
  const [facts, setFacts] = useState<ProfileFact[]>([]);
  const [skillsReady, setSkillsReady] = useState(0);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [userName, setUserName] = useState('');
  const [configLoaded, setConfigLoaded] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!client) return;
    let cancelled = false;
    void api
      .profileList(client)
      .then((payload) => {
        if (!cancelled) setFacts(parseProfileFacts(payload));
      })
      .catch(() => undefined);
    void api
      .presetsList(client)
      .then((payload) => {
        if (!cancelled) setPresets(parsePresets(payload).presets);
      })
      .catch(() => undefined);
    void api
      .configGet(client)
      .then((payload) => {
        if (cancelled) return;
        const values = isRecord(payload) && isRecord(payload.values) ? payload.values : {};
        setUserName(str(values['identity.user_display_name']));
        setConfigLoaded(true);
      })
      .catch(() => undefined);
    if (!device) {
      void api
        .skillsList(client)
        .then((payload) => {
          if (!cancelled) setSkillsReady(readySkills(payload));
        })
        .catch(() => undefined);
    }
    return () => {
      cancelled = true;
    };
  }, [client, device, settingsRevision, tick]);

  const reload = useCallback(() => setTick((n) => n + 1), []);
  const dropFact = useCallback((id: number) => setFacts((list) => list.filter((f) => f.id !== id)), []);

  return {
    unconfirmed: facts.filter((f) => f.unconfirmed),
    fresh: facts.filter((f) => !f.unconfirmed && isNewlyLearned(f)),
    skillsReady,
    presets,
    userName,
    configLoaded,
    reload,
    dropFact,
  };
}
