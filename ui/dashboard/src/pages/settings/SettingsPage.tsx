/**
 * Settings: the page layout and the order things appear in.
 *
 * Order is the main design decision here. Credentials come first, because they are what a
 * first-time user has to reach; then the editable configuration with a jump list over it, because
 * thirty-five rows across nine groups is a lot of scrolling without one; then personality, then the
 * health history. Everything else lives in the small components next to this file, each owning its
 * own requests, its own busy flag and its own error slot — a failure now appears next to the
 * control that failed instead of two screens above it.
 */

import { useMemo } from 'react';

import type { Lang, T } from '../../i18n';
import { groupLabel } from '../../i18n';
import type { IpcClient } from '../../ipc';
import type { StreamPluginStatus } from '../../model';
import { Hero } from '../../ui';
import { ConfigForm, groupId, groupsOf } from './ConfigForm';
import { HealthHistoryTable } from './HealthHistoryTable';
import { IntegrationsSection } from './IntegrationsSection';
import { PersonalityTile } from './PersonalityTile';
import { useHomeTest } from './useHomeTest';
import { useSecrets } from './useSecrets';
import { useSettingsData } from './useSettingsData';
import { useTwitchDeviceFlow } from './useTwitchDeviceFlow';

export interface SettingsPageProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  /** Bumped by `settings.changed`; a change from elsewhere reloads the whole snapshot. */
  revision: number;
  /** Last `twitch.auth.changed` state, so the tile follows an authorisation finished elsewhere. */
  twitchAuthEvent: string | null;
  /** Live plugin connection state, for the OBS tile's state line. */
  plugins: StreamPluginStatus;
  onOpenRemote: () => void;
}

export function SettingsPage({
  t,
  lang,
  client,
  revision,
  twitchAuthEvent,
  plugins,
  onOpenRemote,
}: SettingsPageProps) {
  const data = useSettingsData(client, revision);
  const secrets = useSecrets(client, t, data.secrets, data.pinConfigured, data.reloadSecrets);
  const twitch = useTwitchDeviceFlow(client, t, twitchAuthEvent);
  const homeTest = useHomeTest(client, t);

  const groups = useMemo(() => (data.config ? groupsOf(data.config) : []), [data.config]);
  const disabled = client === null;

  return (
    <div className="page wrap">
      <Hero
        title={t('hero_settings')}
        sub={t('hero_settings_sub')}
        links={
          <button type="button" className="link" disabled={disabled} onClick={data.reload}>
            {t('settings_refresh')}
          </button>
        }
      >
        <p className="hero-note">{t('settings_file')}</p>
      </Hero>

      <IntegrationsSection
        t={t}
        lang={lang}
        client={client}
        secretsFailed={data.secretsFailed}
        pinConfigured={data.pinConfigured}
        secrets={secrets}
        twitch={twitch}
        plugins={plugins}
        onOpenRemote={onOpenRemote}
        homeTest={homeTest}
      />

      <section aria-labelledby="h-config" id="settings-config">
        <div className="rail-head">
          <div>
            <h3 id="h-config" className="rail-title">
              {t('settings_title')}
            </h3>
            {groups.length > 1 && (
              <nav className="jump" aria-label={t('settings_jump')}>
                {groups.map(({ group }) => (
                  <a key={group} className="chip" href={`#${groupId(group)}`}>
                    {groupLabel(t, group)}
                  </a>
                ))}
              </nav>
            )}
          </div>
        </div>

        {data.configFailed && <p className="muted">{t('settings_unavailable')}</p>}
        {/*
          A skeleton rather than a spinner: the shape of a settings form is known before its
          contents are, so showing the shape says more than showing that something is happening.
        */}
        {!data.config && !data.configFailed && (
          <div aria-hidden="true">
            {[0, 1, 2, 3, 4, 5].map((i) => (
              <div key={i} className="skeleton skeleton-row" />
            ))}
          </div>
        )}
        {data.config && (
          <ConfigForm t={t} client={client} config={data.config} onApplied={data.applyValues} />
        )}
      </section>

      <div className="tiles tiles--single">
        <PersonalityTile
          t={t}
          client={client}
          personality={data.personality}
          failed={data.personalityFailed}
          onSaved={data.setPersonality}
        />
      </div>

      <HealthHistoryTable t={t} lang={lang} history={data.history} />
    </div>
  );
}
