import { describe, expect, it } from 'vitest';

import { DICT, type Key, pickLang, statusLabel, translator } from '../i18n';

describe('dictionary', () => {
  it('has the same keys in German and English, all non-empty', () => {
    const de = Object.keys(DICT.de).sort();
    const en = Object.keys(DICT.en).sort();
    expect(en).toEqual(de);
    for (const k of de) {
      expect(DICT.de[k as Key].length, `de.${k}`).toBeGreaterThan(0);
      expect(DICT.en[k as Key].length, `en.${k}`).toBeGreaterThan(0);
    }
  });
  it('covers every privacy mode, capture flag and health status used by the UI', () => {
    for (const k of [
      'privacy_full', 'privacy_balanced', 'privacy_private', 'privacy_offline',
      'capture_microphone', 'capture_camera', 'capture_screen', 'capture_cloud',
      'status_available', 'status_limited', 'status_unavailable',
    ] as Key[]) {
      expect(DICT.de[k]).toBeTruthy();
      expect(DICT.en[k]).toBeTruthy();
    }
  });
});

describe('pickLang', () => {
  it('prefers the query flag, then the browser, then German', () => {
    expect(pickLang('?lang=en', ['de-DE'])).toBe('en');
    expect(pickLang('?lang=de', ['en-US'])).toBe('de');
    expect(pickLang('?lang=fr', ['en-US'])).toBe('en');
    expect(pickLang('', ['en-GB', 'de'])).toBe('en');
    expect(pickLang('', ['fr-FR'])).toBe('de');
    expect(pickLang('', [])).toBe('de');
  });
});

describe('translator', () => {
  it('translates and falls back to German for a missing English string', () => {
    expect(translator('de')('tab_status')).toBe('Status');
    expect(translator('en')('conn_offline')).toBe('core unreachable');
    expect(translator('de')('conn_offline')).toBe('Kern nicht erreichbar');
  });
  it('statusLabel translates known statuses and passes unknown ones through verbatim', () => {
    const t = translator('en');
    expect(statusLabel(t, 'available')).toBe('available');
    expect(statusLabel(t, 'limited')).toBe('limited');
    expect(statusLabel(t, 'weird')).toBe('weird');
    expect(statusLabel(translator('de'), 'unavailable')).toBe('nicht verfügbar');
  });
});
