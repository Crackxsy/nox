/**
 * What Nox knows about you - the facts it picked up from conversations, each one deletable.
 *
 * A memory the user cannot see is one they cannot trust, and one they cannot correct. So the list
 * is plain: the fact in the words it was stored in, when it was learned, and a "Vergessen" button.
 * A fact typed in here counts like "merk dir": it outranks anything Nox inferred on its own.
 *
 * What Nox learned by itself in the last days is marked "neu", so a wrong inference is noticed
 * while it is fresh. A fact learned from a phone message or pasted text is marked "nicht
 * bestätigt": Nox keeps it out of its prompt until the user confirms it here.
 */

import { useCallback, useEffect, useState } from 'react';

import { errorText } from '../../../../shared/errors';
import type { T } from '../../i18n';
import { type IpcClient, api } from '../../ipc';
import { type ProfileFact, isNewlyLearned, parseProfileFacts } from '../../model/profile';
import { Tile } from '../../ui';

export { NEW_FOR_MS, isNewlyLearned, parseProfileFacts } from '../../model/profile';
export type { ProfileFact } from '../../model/profile';

export interface ProfileTileProps {
  t: T;
  lang: string;
  client: IpcClient | null;
}

export function ProfileTile({ t, lang, client }: ProfileTileProps) {
  const [facts, setFacts] = useState<ProfileFact[] | null>(null);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    if (!client) return;
    try {
      const parsed = parseProfileFacts(await api.profileList(client));
      setFacts(parsed);
      setError('');
    } catch (e) {
      setError(errorText(t('error_prefix'), e));
    }
  }, [client, t]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() sets state only after awaiting the core
    void load();
  }, [load]);

  const forget = async (id: number) => {
    if (!client || busy) return;
    setBusy(true);
    try {
      await api.profileForget(client, id);
      setFacts((list) => (list ? list.filter((f) => f.id !== id) : list));
    } catch (e) {
      setError(errorText(t('error_prefix'), e));
    } finally {
      setBusy(false);
    }
  };

  const confirm = async (id: number) => {
    if (!client || busy) return;
    setBusy(true);
    try {
      await api.profileConfirm(client, id);
      setFacts((list) =>
        list ? list.map((f) => (f.id === id ? { ...f, unconfirmed: false } : f)) : list,
      );
    } catch (e) {
      setError(errorText(t('error_prefix'), e));
    } finally {
      setBusy(false);
    }
  };

  const add = async () => {
    const text = draft.trim();
    if (!client || busy || text.length < 3) return;
    setBusy(true);
    try {
      await api.profileAdd(client, text);
      setDraft('');
      await load();
    } catch (e) {
      setError(errorText(t('error_prefix'), e));
    } finally {
      setBusy(false);
    }
  };

  const when = (iso: string) => {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString(lang);
  };

  return (
    <Tile id="profile" title={t('profile_title')} lede={t('profile_hint')}>
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}
      {facts === null ? (
        <p className="muted">{t('profile_loading')}</p>
      ) : facts.length === 0 ? (
        <p className="muted">{t('profile_empty')}</p>
      ) : (
        <ul className="fact-list">
          {facts.map((fact) => (
            <li key={fact.id} className="fact-row">
              <span className="fact-text">
                {fact.text}
                {fact.unconfirmed ? (
                  <span className="badge badge--warn" title={t('profile_unconfirmed_hint')}>
                    {t('profile_unconfirmed')}
                  </span>
                ) : (
                  isNewlyLearned(fact) && <span className="badge">{t('profile_new')}</span>
                )}
              </span>
              <span className="fact-meta">{when(fact.createdAt)}</span>
              <span className="fact-actions">
                {fact.unconfirmed && (
                  <button
                    type="button"
                    className="btn btn--quiet"
                    disabled={busy || !client}
                    onClick={() => void confirm(fact.id)}
                    aria-label={`${t('profile_confirm')}: ${fact.text}`}
                  >
                    {t('profile_confirm')}
                  </button>
                )}
                <button
                  type="button"
                  className="btn btn--quiet"
                  disabled={busy || !client}
                  onClick={() => void forget(fact.id)}
                  aria-label={`${t('profile_forget')}: ${fact.text}`}
                >
                  {t('profile_forget')}
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}
      <div className="field">
        <label htmlFor="profile-add" className="label">
          {t('profile_add_label')}
        </label>
        <input
          id="profile-add"
          className="input"
          value={draft}
          disabled={!client || busy}
          placeholder={t('profile_add_placeholder')}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void add();
          }}
        />
      </div>
      <div className="tile-actions">
        <button
          type="button"
          className="btn"
          disabled={!client || busy || draft.trim().length < 3}
          onClick={() => void add()}
        >
          {t('profile_add')}
        </button>
      </div>
    </Tile>
  );
}
