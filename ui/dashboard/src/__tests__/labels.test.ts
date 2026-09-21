/**
 * The label layer is a contract with the core, so it is tested against the core.
 *
 * `settingLabel` falls back to the raw dotted config path when the dictionary does not know it.
 * That is honest, and it is also exactly how `stream.twitch.min_backoff_s` would end up as a form
 * label on somebody's screen the day the core gains a setting. This test reads the *real*
 * `EDITABLE_PATHS` out of `src/nox/settings/schema.py`, so adding a setting without adding its two
 * labels fails here instead of shipping.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import { DICT, type Key, clipStatusLabel, modeLabel, optionLabel, reasonLine, settingLabel, statusLabel, translator } from '../i18n';
import { MODES, PRIVACY_MODES } from '../model';

const de = translator('de');
const en = translator('en');

/** The core's own allow-list of editable settings, parsed out of its source. */
function editablePaths(): string[] {
  const source = readFileSync(
    join(process.cwd(), '..', '..', 'src', 'nox', 'settings', 'schema.py'),
    'utf8',
  );
  const start = source.indexOf('EDITABLE_PATHS');
  expect(start, 'EDITABLE_PATHS not found in the core schema').toBeGreaterThan(-1);
  const end = source.indexOf('\n}', start);
  return [...source.slice(start, end).matchAll(/^\s*"([a-z0-9_.]+)":/gm)].map((m) => m[1]);
}

describe('every editable setting has a word, in both languages', () => {
  const paths = editablePaths();

  it('finds the core allow-list at all', () => {
    expect(paths.length).toBeGreaterThan(20);
    expect(paths).toContain('identity.name');
  });

  for (const path of paths) {
    it(`${path} is labelled`, () => {
      const key = `setting_${path.replace(/\./g, '_')}`;
      expect(key in DICT.de, `no German label for ${path}`).toBe(true);
      expect(settingLabel(de, path)).not.toBe(path);
      expect(settingLabel(en, path)).not.toBe(path);
    });
  }
});

describe('identifier tables cover what the core actually sends', () => {
  it('labels every assistant mode', () => {
    for (const mode of MODES) {
      expect(modeLabel(de, mode), mode).not.toBe(mode);
      expect(modeLabel(en, mode), mode).not.toBe(mode);
    }
  });

  it('labels every privacy mode', () => {
    for (const mode of PRIVACY_MODES) {
      const key = `privacy_${mode}` as Key;
      expect(key in DICT.de).toBe(true);
    }
  });

  it('labels every health status', () => {
    for (const status of ['available', 'limited', 'unavailable']) {
      expect(statusLabel(de, status)).not.toBe(status);
    }
  });
});

describe('unknown values are shown, never invented', () => {
  it('passes an unknown mode through verbatim', () => {
    expect(modeLabel(de, 'brand_new_mode')).toBe('brand_new_mode');
  });

  it('passes an unknown clip status through instead of calling it "neu"', () => {
    expect(clipStatusLabel(de, 'quarantined')).toBe('quarantined');
    expect(clipStatusLabel(de, 'quarantined')).not.toBe(de('clips_status_new'));
  });

  it('passes an unknown enum option through verbatim', () => {
    expect(optionLabel(de, 'voice.tts.engine', 'brand_new_engine')).toBe('brand_new_engine');
  });
});

describe('reason translation keeps the original', () => {
  it('translates a known reason and hands back the core wording as the detail', () => {
    const line = reasonLine(de, 'de', 'deterministic rules, always available');
    expect(line.text).toBe('Regelbasierte Antworten, immer verfügbar');
    expect(line.original).toBe('deterministic rules, always available');
  });

  it('names the model in a parameterised reason', () => {
    expect(reasonLine(de, 'de', 'model llama3.2:3b available').text).toBe(
      'Modell llama3.2:3b geladen',
    );
    expect(reasonLine(de, 'de', 'model llama3.2:3b not pulled').text).toBe(
      'Modell llama3.2:3b ist nicht heruntergeladen',
    );
  });

  it('turns a round-trip reason into a version and a readable latency', () => {
    const line = reasonLine(de, 'de', '2.1.273 (Claude Code); round-trip 2498 ms');
    expect(line.text).toBe('Version 2.1.273 (Claude Code), Antwortzeit 2,5 s');
    expect(line.original).toBe('2.1.273 (Claude Code); round-trip 2498 ms');
  });

  it('never puts a machine path in the label', () => {
    for (const path of ['C:\\Users\\x\\vault', '/home/x/vault']) {
      const line = reasonLine(de, 'de', path);
      expect(line.text).toBe('Ordner erreichbar');
      expect(line.original).toBe(path);
    }
  });

  it('shows an unknown reason verbatim with no second line', () => {
    const line = reasonLine(de, 'de', 'something the table has never seen');
    expect(line.text).toBe('something the table has never seen');
    expect(line.original).toBeNull();
  });

  it('says nothing at all when the core said nothing', () => {
    expect(reasonLine(de, 'de', '')).toEqual({ text: '', original: null });
  });
});

describe('the two dictionaries stay in step', () => {
  it('has the same keys on both sides', () => {
    expect(Object.keys(DICT.en).sort()).toEqual(Object.keys(DICT.de).sort());
  });

  it('has no empty string anywhere', () => {
    for (const [key, value] of Object.entries(DICT.de)) expect(value.length, key).toBeGreaterThan(0);
    for (const [key, value] of Object.entries(DICT.en)) expect(value.length, key).toBeGreaterThan(0);
  });

  it('spells the kill switch one way', () => {
    const german = Object.values(DICT.de).join(' ');
    expect(german).not.toMatch(/Notaus \(/);
    expect(german).not.toMatch(/Safe Mode/);
  });
});
