/**
 * The command palette's matcher (Spec UX-Neubau §4): pure, synchronous, no model involved, so a
 * keystroke has its results well inside the 50 ms budget.
 *
 * Four groups, always in this order:
 *  1. "Sofort erledigen" - lights, sockets and scenes Nox already knows, matched by name and room,
 *     plus the sentence as a command for the core's own deterministic intent layer (`home.command`);
 *  2. "Abläufe" - presets by name or by one of their trigger sentences;
 *  3. "Gehe zu" - every page and every setting;
 *  4. "Nox fragen" - always last: the text goes to the conversation.
 */

import { type HomeEntity, type Preset, domainOf } from '../model';
import type { Route } from './routes';

export type PaletteGroup = 'now' | 'flows' | 'goto' | 'ask';

export type PaletteAction =
  | { kind: 'home'; entityId: string; domain: 'light' | 'switch'; on: boolean }
  | { kind: 'scene'; entityId: string }
  | { kind: 'command'; text: string }
  | { kind: 'preset'; id: string }
  | { kind: 'goto'; route: Route }
  | { kind: 'ask'; text: string };

export interface PaletteItem {
  id: string;
  group: PaletteGroup;
  label: string;
  detail?: string;
  action: PaletteAction;
}

export interface GotoEntry {
  label: string;
  detail?: string;
  /** Extra words the entry is found by (old names like "Audit", the setting's path). */
  keywords?: string;
  route: Route;
}

export interface PaletteWords {
  turnOn: (name: string) => string;
  turnOff: (name: string) => string;
  scene: (name: string) => string;
  command: (text: string) => string;
  ask: (text: string) => string;
}

export interface PaletteSources {
  entities: readonly HomeEntity[];
  /** Whether the core's home layer can take a sentence (Home Assistant connected). */
  homeReady: boolean;
  presets: readonly Preset[];
  pages: readonly GotoEntry[];
  words: PaletteWords;
  /** Paired devices may not run presets (`presets.activate` is local-only). */
  canRunPresets: boolean;
}

const OFF_WORDS = ['aus', 'ausschalten', 'ausmachen', 'off', 'turn off', 'switch off'];
const ON_WORDS = ['an', 'ein', 'einschalten', 'anmachen', 'on', 'turn on', 'switch on'];

export function normalize(text: string): string {
  return text
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/ß/g, 'ss')
    .replace(/[^a-z0-9 ]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function words(text: string): string[] {
  const n = normalize(text);
  return n ? n.split(' ') : [];
}

/** Every query word (minus on/off words) is found somewhere in the haystack. */
function matches(query: string[], haystack: string): boolean {
  const hay = normalize(haystack);
  const meaningful = query.filter((w) => !OFF_WORDS.includes(w) && !ON_WORDS.includes(w));
  if (meaningful.length === 0) return false;
  return meaningful.every((w) => hay.includes(w));
}

function wantedState(query: string[]): boolean | null {
  if (query.some((w) => OFF_WORDS.includes(w))) return false;
  if (query.some((w) => ON_WORDS.includes(w))) return true;
  return null;
}

const LIMIT = { now: 5, flows: 4, goto: 6 } as const;

export function paletteItems(rawQuery: string, src: PaletteSources): PaletteItem[] {
  const query = words(rawQuery);
  const text = rawQuery.trim();
  const items: PaletteItem[] = [];

  if (query.length > 0) {
    // 1. Sofort erledigen
    const wanted = wantedState(query);
    const now: PaletteItem[] = [];
    for (const entity of src.entities) {
      const domain = domainOf(entity.entityId);
      if (!matches(query, `${entity.name} ${entity.area} ${domain === 'light' ? 'licht lampe light' : ''}`)) continue;
      const where = entity.area ? `${entity.name}, ${entity.area}` : entity.name;
      if (domain === 'scene') {
        now.push({
          id: `scene:${entity.entityId}`,
          group: 'now',
          label: src.words.scene(where),
          action: { kind: 'scene', entityId: entity.entityId },
        });
      } else if (domain === 'light' || domain === 'switch') {
        const on = wanted ?? entity.state !== 'on';
        now.push({
          id: `home:${entity.entityId}:${on ? 'on' : 'off'}`,
          group: 'now',
          label: on ? src.words.turnOn(where) : src.words.turnOff(where),
          action: { kind: 'home', entityId: entity.entityId, domain, on },
        });
      }
    }
    items.push(...now.slice(0, LIMIT.now));
    if (src.homeReady && text.length >= 3) {
      items.push({
        id: 'command',
        group: 'now',
        label: src.words.command(text),
        action: { kind: 'command', text },
      });
    }

    // 2. Abläufe
    if (src.canRunPresets) {
      const flows = src.presets
        .filter((p) => p.enabled && matches(query, `${p.name} ${p.id} ${p.triggers.phrases.join(' ')}`))
        .slice(0, LIMIT.flows)
        .map<PaletteItem>((p) => ({
          id: `preset:${p.id}`,
          group: 'flows',
          label: p.name || p.id,
          detail: p.triggers.phrases[0],
          action: { kind: 'preset', id: p.id },
        }));
      items.push(...flows);
    }
  }

  // 3. Gehe zu - with an empty query the areas themselves, as a starting point.
  const pages = query.length === 0
    ? src.pages.slice(0, 4)
    : src.pages.filter((p) => matches(query, `${p.label} ${p.detail ?? ''} ${p.keywords ?? ''}`)).slice(0, LIMIT.goto);
  items.push(
    ...pages.map<PaletteItem>((p, i) => ({
      id: `goto:${i}:${p.label}`,
      group: 'goto',
      label: p.label,
      detail: p.detail,
      action: { kind: 'goto', route: p.route },
    })),
  );

  // 4. Nox fragen - always last, whenever there is something to ask.
  if (text) {
    items.push({ id: 'ask', group: 'ask', label: src.words.ask(text), action: { kind: 'ask', text } });
  }
  return items;
}
