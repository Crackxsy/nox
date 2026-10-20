/**
 * What Nox knows about the person (`memory.profile.*`): the fact rows, which ones Nox learned by
 * itself recently, and which wait for a confirmation before Nox uses them. Pure parsing; the
 * Settings tile and Heute's "Wartet auf dich" both read it.
 */

import { isRecord, str } from '../../../shared/guards';

export interface ProfileFact {
  id: number;
  type: string;
  text: string;
  createdAt: string;
  /** Inferred by Nox from a conversation, not typed in by the user. */
  learned: boolean;
  /** From a phone message or pasted text; not used until confirmed. */
  unconfirmed: boolean;
}

/** How long a fact Nox learned by itself is marked as new. */
export const NEW_FOR_MS = 7 * 24 * 60 * 60 * 1000;

/** Learned by Nox (not typed in) within `NEW_FOR_MS` of `now`. */
export function isNewlyLearned(fact: ProfileFact, now: number = Date.now()): boolean {
  const created = new Date(fact.createdAt).getTime();
  return fact.learned && !Number.isNaN(created) && now - created < NEW_FOR_MS;
}

/** `memory.profile.list` reply -> facts; anything malformed is dropped rather than guessed. */
export function parseProfileFacts(payload: unknown): ProfileFact[] {
  if (!isRecord(payload) || !Array.isArray(payload.items)) return [];
  const facts: ProfileFact[] = [];
  for (const entry of payload.items) {
    if (!isRecord(entry) || typeof entry.id !== 'number') continue;
    const text = str(entry.text);
    if (!text) continue;
    facts.push({
      id: entry.id,
      type: str(entry.type, 'user'),
      text,
      createdAt: str(entry.created_at),
      learned: entry.learned === true,
      unconfirmed: entry.unconfirmed === true,
    });
  }
  return facts;
}

