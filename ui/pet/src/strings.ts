/**
 * The pet window's eight strings.
 *
 * They used to be inline and bilingual ("Kein Sitzungs-Token – … / no session token …"), which
 * showed both languages to everyone and matched neither the dashboard's dictionary nor its tone.
 * The language decision itself is shared (`ui/shared/lang.ts`); the words are local, because eight
 * strings do not need a dictionary module.
 */

import { type Lang, pickLang } from '../../shared/lang';

export type { Lang };
export { pickLang };

const DE = {
  no_token: 'Kein Sitzungs-Token. Nox-Fenster über das Infobereich-Symbol öffnen.',
  auth_denied: 'Authentifizierung abgelehnt',
  capture_unknown: 'Nox ist nicht erreichbar – Aufnahmestatus unbekannt',
  capture_none: 'Keine Aufnahme',
  capture_active: 'Aktiv',
  capture_microphone: 'Mikrofon',
  capture_screen: 'Bildschirm',
  capture_camera: 'Kamera',
  capture_cloud: 'Cloud-Anfrage',
  privacy: 'Privatsphäre',
  muted: 'stumm',
  offline: 'offline',
} as const;

export type PetKey = keyof typeof DE;

const EN: Record<PetKey, string> = {
  no_token: 'No session token. Open the Nox window from the tray icon.',
  auth_denied: 'Authentication denied',
  capture_unknown: 'Nox is unreachable – capture state unknown',
  capture_none: 'No capture',
  capture_active: 'Active',
  capture_microphone: 'Microphone',
  capture_screen: 'Screen',
  capture_camera: 'Camera',
  capture_cloud: 'Cloud request',
  privacy: 'Privacy',
  muted: 'muted',
  offline: 'offline',
};

export type PetT = (key: PetKey) => string;

export function petTranslator(lang: Lang): PetT {
  const table = lang === 'en' ? EN : DE;
  return (key: PetKey) => table[key];
}
