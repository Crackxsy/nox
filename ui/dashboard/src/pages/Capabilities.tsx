/**
 * Geräte & Fähigkeiten › Fähigkeiten: what Nox can do beyond what it ships with.
 *
 * Plain first, then a visible step down (Spec UX-Neubau §3): Abläufe (formerly Presets) and
 * Wächter for everyone; under "Für Fortgeschrittene" the skills Nox taught itself and the
 * extensions (MCP servers) other people wrote. Each section loads on its own, so one missing
 * extension does not blank the page; "Aktualisieren" re-reads all of them. The PIN status is read
 * once per connection, so the add form and the skill secrets know whether a change needs one.
 */

import { useCallback, useState } from 'react';

import type { Lang } from '../../../shared/lang';
import { useRefreshOnConnect } from '../hooks';
import type { T } from '../i18n';
import { type IpcClient, api } from '../ipc';
import { parsePinConfigured } from '../model';
import { McpSection } from './capabilities/McpSection';
import { SkillsSection } from './capabilities/SkillsSection';
import { WatchSection } from './capabilities/WatchSection';
import { PresetsPage } from './Presets';

export interface CapabilitiesPageProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  /** Leave the Abläufe out (tests of the other sections). */
  withPresets?: boolean;
}

export function CapabilitiesPage({ t, lang, client, withPresets = true }: CapabilitiesPageProps) {
  const [revision, setRevision] = useState(0);
  const [pinConfigured, setPinConfigured] = useState(false);

  const loadPin = useCallback(async (c: IpcClient, cancelled: () => boolean) => {
    try {
      const configured = parsePinConfigured(await api.pinStatus(c));
      if (!cancelled()) setPinConfigured(configured);
    } catch {
      // Unknown means "send none": the core's own refusal then says a PIN is needed.
      if (!cancelled()) setPinConfigured(false);
    }
  }, []);
  useRefreshOnConnect(client, loadPin);

  return (
    <div className="caps">
      <p className="lede">
        {t('caps_lede')}{' '}
        <button
          type="button"
          className="btn btn--sm btn--plain"
          disabled={client === null}
          onClick={() => setRevision((r) => r + 1)}
        >
          {t('caps_refresh')}
        </button>
      </p>
      {withPresets && <PresetsPage key={revision} t={t} client={client} />}
      <WatchSection t={t} lang={lang} client={client} revision={revision} />
      <section className="tier" aria-labelledby="h-advanced">
        <h2 id="h-advanced" className="sec-title">
          {t('ux_advanced')}
        </h2>
        <p className="hint">{t('ux_advanced_hint')}</p>
        <SkillsSection t={t} lang={lang} client={client} revision={revision} pinConfigured={pinConfigured} />
        <McpSection t={t} client={client} revision={revision} pinConfigured={pinConfigured} />
      </section>
    </div>
  );
}
