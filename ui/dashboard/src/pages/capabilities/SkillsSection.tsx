/**
 * "Selbst gelernte Fähigkeiten": the skills Nox built for itself on request.
 *
 * Learning starts in chat or by voice, not here; this section shows how far each one got and lets
 * the user activate, deactivate or forget it. While anything is still being built the list is
 * polled every three seconds and polling stops when nothing is. When the core cannot learn right
 * now (no Claude Code, switched off, ...) the section says so in the core's words.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { errorText } from '../../../../shared/errors';
import type { Lang } from '../../../../shared/lang';
import { type T, fill } from '../../i18n';
import { type IpcClient, IpcError, api } from '../../ipc';
import {
  type ActionResult,
  type SkillList,
  anyBuilding,
  parseActionResult,
  parseSkills,
  pinErrorKey,
} from '../../model';
import { Tile } from '../../ui';
import { SkillCard } from './SkillCard';
import { loadError, useReload } from './shared';

/** How often the list is re-read while a skill is being built. */
export const SKILLS_POLL_MS = 3000;

export interface SkillsSectionProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  revision: number;
  pinConfigured: boolean;
}

export function SkillsSection({ t, lang, client, revision, pinConfigured }: SkillsSectionProps) {
  const [list, setList] = useState<SkillList | null>(null);
  const [loadFailed, setLoadFailed] = useState('');
  const [error, setError] = useState('');
  const [refusal, setRefusal] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const busyRef = useRef<string | null>(null);

  const load = useCallback(
    async (c: IpcClient, cancelled: () => boolean = () => false) => {
      try {
        const parsed = parseSkills(await api.skillsList(c));
        if (cancelled()) return;
        setList(parsed);
        setLoadFailed('');
      } catch (e) {
        if (!cancelled()) setLoadFailed(loadError(t, e));
      }
    },
    [t],
  );
  useReload(client, revision, load);

  const building = anyBuilding(list);
  useEffect(() => {
    if (!client || !building) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void load(client, () => cancelled);
    }, SKILLS_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [client, building, load]);

  /** One request at a time; a thrown error becomes the section's error line. */
  const guard = async <R,>(label: string, fn: (c: IpcClient) => Promise<R>): Promise<R | null> => {
    if (!client || busyRef.current !== null) return null;
    busyRef.current = label;
    setBusy(label);
    setError('');
    setRefusal('');
    try {
      return await fn(client);
    } catch (e) {
      setError(errorText(t('error_prefix'), e));
      return null;
    } finally {
      busyRef.current = null;
      setBusy(null);
    }
  };

  const activate = (id: string, pin: string): Promise<ActionResult | null> =>
    guard(`activate:${id}`, async (c) => {
      const result = parseActionResult(await api.skillActivate(c, id, pin));
      if (result.ok) await load(c);
      return result;
    });

  const simple = (label: string, fn: (c: IpcClient) => Promise<unknown>) => {
    void guard(label, async (c) => {
      const result = parseActionResult(await fn(c));
      if (!result.ok) setRefusal(fill(t('caps_refused'), result.reason || t('unknown')));
      await load(c);
    });
  };

  const setSecret = async (name: string, value: string, pin: string): Promise<string> => {
    const message = await guard(`secret:${name}`, async (c) => {
      try {
        await api.secretSet(c, name, value, pin);
      } catch (e) {
        const key = e instanceof IpcError ? pinErrorKey(e.code, e.message, pin !== '') : null;
        return key ? t(key) : errorText(t('error_prefix'), e);
      }
      await load(c);
      return '';
    });
    return message ?? '';
  };

  const skills = list?.skills ?? [];

  return (
    <section className="caps-section" aria-labelledby="h-caps-skills">
      <div className="rail-head">
        <div>
          <h3 id="h-caps-skills" className="rail-title">
            {t('caps_skills_title')}
          </h3>
          <p className="rail-sub">{t('caps_skills_lede')}</p>
        </div>
      </div>

      {loadFailed && (
        <p role="alert" className="alert">
          {loadFailed}
        </p>
      )}
      {list && !list.available && (
        <p className="hint caps-emph">
          {fill(t('caps_skills_unavailable'), list.reason || t('unknown'))}
        </p>
      )}
      {refusal && (
        <p role="alert" className="alert">
          {refusal}
        </p>
      )}
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      <div className="tiles tiles--single">
        {list && skills.length === 0 && (
          <Tile level={4} title={t('caps_skills_empty')}>
            <p className="muted">{t('caps_skills_empty_hint')}</p>
          </Tile>
        )}
        {skills.map((skill) => (
          <SkillCard
            key={skill.id}
            t={t}
            lang={lang}
            skill={skill}
            busy={busy}
            connected={client !== null}
            pinConfigured={pinConfigured}
            onActivate={activate}
            onDeactivate={(id) => simple(`deactivate:${id}`, (c) => api.skillDeactivate(c, id))}
            onForget={(id) => simple(`forget:${id}`, (c) => api.skillForget(c, id))}
            onSetSecret={setSecret}
          />
        ))}
      </div>
    </section>
  );
}
