/**
 * "Gehe zu" in the command palette: every area, tab and Settings page, and every setting by its
 * label (with the dotted path as a keyword, so "voice.tts" finds it too).
 */

import { type T, settingLabel } from '../i18n';
import { PLACED_PATHS, SETTINGS_LAYOUT, pageOfPath } from '../areas/settings/layout';
import type { GotoEntry } from './palette';

/** Every page and every setting, for "Gehe zu" in the palette. */
export function gotoIndex(t: T): GotoEntry[] {
  const pages: GotoEntry[] = [
    { label: t('ux_area_today'), keywords: 'status start', route: { area: 'today' } },
    { label: t('ux_tab_talk'), detail: t('ux_area_nox'), keywords: 'chat', route: { area: 'nox', tab: 'talk' } },
    { label: t('ux_tab_views'), detail: t('ux_area_nox'), keywords: 'board', route: { area: 'nox', tab: 'views' } },
    { label: t('ux_tab_clips'), detail: t('ux_area_nox'), route: { area: 'nox', tab: 'clips' } },
    {
      label: t('ux_tab_history'),
      detail: t('ux_area_nox'),
      keywords: 'audit log',
      route: { area: 'nox', tab: 'history' },
    },
    {
      label: t('ux_tab_devices'),
      detail: t('ux_area_devices'),
      keywords: 'remote koppeln pair handy phone zuhause home stream obs',
      route: { area: 'devices', tab: 'devices' },
    },
    {
      label: t('ux_tab_skills'),
      detail: t('ux_area_devices'),
      keywords: 'presets ablaeufe flows waechter watchers mcp skills',
      route: { area: 'devices', tab: 'skills' },
    },
    { label: t('ux_area_settings'), route: { area: 'settings', page: null } },
  ];
  for (const entry of SETTINGS_LAYOUT) {
    pages.push({ label: t(entry.title), detail: t('ux_area_settings'), route: { area: 'settings', page: entry.id } });
  }
  for (const path of PLACED_PATHS) {
    const page = pageOfPath(path);
    const title = SETTINGS_LAYOUT.find((p) => p.id === page)?.title;
    pages.push({
      label: settingLabel(t, path),
      detail: title ? t(title) : undefined,
      keywords: path,
      route: { area: 'settings', page },
    });
  }
  return pages;
}
