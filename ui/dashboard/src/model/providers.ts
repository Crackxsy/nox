/** `ai.providers` — the provider rail on the Status page and the display names the chat byline uses. */

import { bool, isRecord, str, stringList } from '../../../shared/guards';

export interface Provider {
  id: string;
  displayName: string;
  local: boolean;
  roles: string[];
  status: string;
  reason: string;
}

/** `ai.providers` response: a list of ProviderInfo, carried as `{providers: [...]}`. */
export function parseProviders(payload: unknown): Provider[] | null {
  if (!isRecord(payload)) return null;
  const list = Array.isArray(payload.providers)
    ? payload.providers
    : Array.isArray(payload.items)
      ? payload.items
      : null;
  if (!list) return null;
  const out: Provider[] = [];
  for (const entry of list) {
    if (!isRecord(entry)) continue;
    const id = str(entry.id);
    if (!id) continue;
    out.push({
      id,
      displayName: str(entry.display_name, id),
      local: bool(entry.local),
      roles: stringList(entry.roles),
      status: str(entry.status, 'unavailable'),
      reason: str(entry.reason),
    });
  }
  return out;
}

/** The provider's own display name ("Ollama (llama3.2:3b)"), or the bare id when it is unknown. */
export function providerName(providers: Provider[] | null, id: string): string {
  return providers?.find((p) => p.id === id)?.displayName || id;
}
