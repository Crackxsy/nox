/**
 * The command palette's matcher and the routes behind it: pure functions, so every rule the spec
 * states (group order, "Nox fragen" last, on/off words, old tab ids) is pinned without a DOM.
 */

import { describe, expect, it } from 'vitest';

import type { HomeEntity, Preset } from '../model';
import { type PaletteSources, normalize, paletteItems } from '../shell/palette';
import { hashToRoute, legacyRoute, routeToHash } from '../shell/routes';

const entity = (entityId: string, name: string, area: string, state = 'off'): HomeEntity => ({
  entityId,
  name,
  area,
  state,
  attributes: {},
});

const preset = (id: string, name: string, phrase: string, enabled = true): Preset => ({
  id,
  name,
  enabled,
  triggers: { phrases: [phrase], at: '', days: [], on_process_start: '', on_process_end: '' },
  steps: [],
});

function sources(overrides: Partial<PaletteSources> = {}): PaletteSources {
  return {
    entities: [
      entity('light.wz', 'Deckenlampe', 'Wohnzimmer', 'on'),
      entity('switch.kaffee', 'Kaffeemaschine', 'Küche'),
      entity('scene.film', 'Film', 'Wohnzimmer'),
    ],
    homeReady: true,
    presets: [preset('stream', 'Stream vorbereiten', 'lass uns streamen'), preset('off', 'Aus', 'aus', false)],
    pages: [
      { label: 'Verlauf', detail: 'Nox', keywords: 'audit', route: { area: 'nox', tab: 'history' } },
      { label: 'Stimme', detail: 'Einstellungen', route: { area: 'settings', page: 'voice' } },
    ],
    canRunPresets: true,
    words: {
      turnOn: (n) => `${n} einschalten`,
      turnOff: (n) => `${n} ausschalten`,
      scene: (n) => `Szene ${n}`,
      command: (x) => `Befehl: ${x}`,
      ask: (x) => `Fragen: ${x}`,
    },
    ...overrides,
  };
}

describe('paletteItems', () => {
  it('keeps the four groups in order, with "Nox fragen" always last', () => {
    const items = paletteItems('wohnzimmer', sources());
    const groups = items.map((i) => i.group);
    expect(groups.indexOf('now')).toBeLessThan(groups.indexOf('ask'));
    expect(items[items.length - 1]!.group).toBe('ask');
    expect(items[items.length - 1]!.label).toBe('Fragen: wohnzimmer');
  });

  it('reads on and off words, and otherwise flips the current state', () => {
    const off = paletteItems('licht wohnzimmer aus', sources()).find((i) => i.action.kind === 'home');
    expect(off?.action).toEqual({ kind: 'home', entityId: 'light.wz', domain: 'light', on: false });
    const on = paletteItems('kaffee an', sources()).find((i) => i.action.kind === 'home');
    expect(on?.action).toEqual({ kind: 'home', entityId: 'switch.kaffee', domain: 'switch', on: true });
    const flip = paletteItems('kaffeemaschine', sources()).find((i) => i.action.kind === 'home');
    expect(flip?.label).toBe('Kaffeemaschine, Küche einschalten');
  });

  it('offers scenes, and the sentence as a home command only while Home Assistant is there', () => {
    expect(paletteItems('film', sources()).some((i) => i.action.kind === 'scene')).toBe(true);
    expect(paletteItems('film ab', sources()).some((i) => i.action.kind === 'command')).toBe(true);
    expect(paletteItems('film ab', sources({ homeReady: false })).some((i) => i.action.kind === 'command')).toBe(false);
  });

  it('finds flows by name or trigger sentence, never disabled ones, and none on a paired device', () => {
    expect(paletteItems('streamen', sources()).find((i) => i.group === 'flows')?.label).toBe('Stream vorbereiten');
    expect(paletteItems('aus', sources()).some((i) => i.action.kind === 'preset' && i.action.id === 'off')).toBe(false);
    expect(paletteItems('streamen', sources({ canRunPresets: false })).some((i) => i.group === 'flows')).toBe(false);
  });

  it('finds pages by their old names too, and shows the areas for an empty query', () => {
    expect(paletteItems('audit', sources()).find((i) => i.group === 'goto')?.label).toBe('Verlauf');
    const empty = paletteItems('', sources());
    expect(empty.every((i) => i.group === 'goto')).toBe(true);
    expect(empty.some((i) => i.group === 'ask')).toBe(false);
  });

  it('normalises umlauts, case and punctuation', () => {
    expect(normalize('Küche, BITTE!')).toBe('kuche bitte');
    expect(paletteItems('KUCHE', sources()).some((i) => i.label.startsWith('Kaffeemaschine'))).toBe(true);
  });
});

describe('routes', () => {
  it('round-trips every route through the fragment', () => {
    for (const route of [
      { area: 'today' as const },
      { area: 'nox' as const, tab: 'views' as const },
      { area: 'devices' as const, tab: 'skills' as const },
      { area: 'settings' as const, page: null },
      { area: 'settings' as const, page: 'voice' as const },
    ]) {
      expect(hashToRoute(routeToHash(route))).toEqual(route);
    }
  });

  it('ignores fragments that are not routes (the skip link, the token)', () => {
    expect(hashToRoute('#main')).toBeNull();
    expect(hashToRoute('#token=abc')).toBeNull();
    expect(hashToRoute('')).toBeNull();
  });

  it('falls back inside a known area, and sends old tab ids where their content lives now', () => {
    expect(hashToRoute('#/nox/nonsense')).toEqual({ area: 'nox', tab: 'talk' });
    expect(hashToRoute('#/settings/nonsense')).toEqual({ area: 'settings', page: null });
    expect(legacyRoute('remote')).toEqual({ area: 'devices', tab: 'devices' });
    expect(legacyRoute('presets')).toEqual({ area: 'devices', tab: 'skills' });
    expect(legacyRoute('board')).toEqual({ area: 'nox', tab: 'views' });
    expect(legacyRoute('status')).toEqual({ area: 'today' });
    expect(hashToRoute('#/whatever')).toEqual({ area: 'today' });
  });
});
