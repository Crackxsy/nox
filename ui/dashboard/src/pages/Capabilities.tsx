/**
 * "Fähigkeiten": what Nox can do beyond what it ships with.
 *
 * Three sections, each loading on its own so one missing extension does not blank the page:
 * other people's MCP servers (added and approved here), skills Nox taught itself (activated
 * here), and watchers (created in chat or by voice, reviewed and deleted here). "Aktualisieren"
 * re-reads all three. The PIN status is read once per connection, so the add form and the skill
 * secrets know whether a secret change has to carry one.
 */

import { useCallback, useState } from 'react';

import type { Lang } from '../../../shared/lang';
import { useRefreshOnConnect } from '../hooks';
import type { T } from '../i18n';
import { type IpcClient, api } from '../ipc';
import { parsePinConfigured } from '../model';
import { Hero } from '../ui';
import { McpSection } from './capabilities/McpSection';
import { SkillsSection } from './capabilities/SkillsSection';
import { WatchSection } from './capabilities/WatchSection';

export interface CapabilitiesPageProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
}

export function CapabilitiesPage({ t, lang, client }: CapabilitiesPageProps) {
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
    <>
      <Hero
        title={t('caps_title')}
        sub={t('caps_lede')}
        links={
          <button
            type="button"
            className="link"
            disabled={client === null}
            onClick={() => setRevision((r) => r + 1)}
          >
            {t('caps_refresh')}
          </button>
        }
      />
      <McpSection t={t} client={client} revision={revision} pinConfigured={pinConfigured} />
      <SkillsSection
        t={t}
        lang={lang}
        client={client}
        revision={revision}
        pinConfigured={pinConfigured}
      />
      <WatchSection t={t} lang={lang} client={client} revision={revision} />
    </>
  );
}
