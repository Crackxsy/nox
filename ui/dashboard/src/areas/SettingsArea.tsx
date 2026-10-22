/**
 * Einstellungen: eight pages instead of one long form (Spec UX-Neubau §3, §6, §7).
 *
 * The overview lists the pages with their current state in words ("Standard, Mikrofon an"), so a
 * person seldom has to open one. Each page shows the few things that matter, then "Mehr Optionen",
 * and the components inside keep identifiers behind "Technische Angaben". Settings the layout does
 * not place are still listed (System › Mehr Optionen), so nothing the core offers is lost.
 *
 * A paired device sees every page read-only (the core refuses administration to it); only the
 * appearance, which lives in this browser, stays changeable there.
 */

import { type Key, type Lang, type T, fill, modeHeadline, privacyLabel} from '../i18n';
import type { IpcClient } from '../ipc';
import type { DashboardState } from '../model';
import { ConfigForm } from '../pages/settings/ConfigForm';
import { ConsentTile } from '../pages/settings/ConsentTile';
import { HealthHistoryTable } from '../pages/settings/HealthHistoryTable';
import { IntegrationsSection } from '../pages/settings/IntegrationsSection';
import { LocalAiTile } from '../pages/settings/LocalAiTile';
import { PersonalityTile } from '../pages/settings/PersonalityTile';
import { ProfileTile } from '../pages/settings/ProfileTile';
import { useHomeTest } from '../pages/settings/useHomeTest';
import { useSecrets } from '../pages/settings/useSecrets';
import { useSettingsData } from '../pages/settings/useSettingsData';
import { useTwitchDeviceFlow } from '../pages/settings/useTwitchDeviceFlow';
import { VoiceModelTile } from '../pages/settings/VoiceModelTile';
import { StreamSetup } from '../pages/StreamSetup';
import { ModeChoice, PrivacyChoice, captureLine } from '../shell/choices';
import { Icon } from '../shell/icons';
import { LocalOnly } from '../shell/LocalOnly';
import { troublesOf } from '../shell/presence';
import type { Route, SettingsPageId } from '../shell/routes';
import { type ThemePref, THEME_PREFS } from '../theme';
import { SETTINGS_LAYOUT, layoutOf, unplacedPaths } from './settings/layout';
import { RemoteMaintenance } from './settings/RemoteMaintenance';
import { HealthTable, ProvidersList, VoiceFacts } from './settings/SystemParts';

const THEME_LABEL: Record<ThemePref, Key> = {
  system: 'theme_system',
  light: 'theme_light',
  dark: 'theme_dark',
};

export interface SettingsAreaProps {
  t: T;
  lang: Lang;
  state: DashboardState;
  client: IpcClient | null;
  device: boolean;
  page: SettingsPageId | null;
  navigate: (route: Route) => void;
  theme: ThemePref;
  onTheme: (pref: ThemePref) => void;
  providersFailed: boolean;
  onRefreshProviders: () => void;
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

export default function SettingsArea(props: SettingsAreaProps) {
  const { t, lang, state, client, device, page, navigate } = props;
  const data = useSettingsData(client, state.settingsRevision);
  const secrets = useSecrets(client, t, data.secrets, data.pinConfigured, data.reloadSecrets);
  const twitch = useTwitchDeviceFlow(client, t, state.twitchAuth);
  const homeTest = useHomeTest(client, t);
  const values = data.config?.values ?? {};

  /** One line per page: how it is set right now, in words. */
  const summary = (id: SettingsPageId): string => {
    const words: string[] = [];
    switch (id) {
      case 'privacy':
        if (state.privacyMode) words.push(privacyLabel(t, state.privacyMode));
        if (state.mode) words.push(modeHeadline(t, state.mode));
        break;
      case 'voice': {
        const engine = str(values['voice.tts.engine']);
        const wake = str(values['voice.stt.wake_word']);
        if (engine) words.push(engine.charAt(0).toUpperCase() + engine.slice(1));
        if (wake) words.push(fill(t('ux_set_voice_wake'), wake));
        break;
      }
      case 'stream':
        words.push(
          state.stream.session.plugins.obs === 'connected' ? t('ux_set_stream_obs_on') : t('ux_set_stream_obs_off'),
        );
        if (state.twitchAuth === 'authorized') words.push(t('ux_set_stream_twitch_on'));
        break;
      case 'you':
        if (str(values['identity.user_display_name'])) words.push(str(values['identity.user_display_name']));
        break;
      case 'nox':
        if (str(values['identity.name'])) words.push(str(values['identity.name']));
        break;
      case 'home':
        words.push(state.home.connected ? t('ux_set_home_on') : t('ux_set_home_off'));
        break;
      case 'ai': {
        const ok = (state.providers ?? []).filter((p) => p.status === 'available').length;
        if (state.providers) words.push(fill(t('ux_set_ai_count'), ok));
        break;
      }
      case 'system': {
        const down = troublesOf(state).length;
        words.push(down === 0 ? t('ux_health_ok') : fill(down === 1 ? t('ux_health_one') : t('ux_health_many'), down));
        break;
      }
    }
    return words.join(', ');
  };

  if (page === null) {
    return (
      <div className="page">
        <header className="page-h">
          <h1>{t('ux_area_settings')}</h1>
          <p>{t('ux_settings_lede')}</p>
        </header>
        {device && <p className="note device-note">{t('device_admin_note')}</p>}
        <ul className="list list--rows list--nav" aria-label={t('ux_area_settings')}>
          {SETTINGS_LAYOUT.map((entry) => (
            <li key={entry.id} className="row">
              <a
                className="row-link"
                href={`#/settings/${entry.id}`}
                onClick={(e) => {
                  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                  e.preventDefault();
                  navigate({ area: 'settings', page: entry.id });
                }}
              >
                <span className="grow">
                  <span className="row-t">{t(entry.title)}</span>
                  <span className="row-d">{summary(entry.id) || t(entry.lede)}</span>
                </span>
                <Icon name="chevron" />
              </a>
            </li>
          ))}
        </ul>
      </div>
    );
  }

  const layout = layoutOf(page);
  const config = data.config;
  const forms = config
    ? layout.sections.map((section, i) => (
        <ConfigForm
          key={`${page}-${i}`}
          t={t}
          client={client}
          config={config}
          onApplied={data.applyValues}
          paths={section.paths}
          morePaths={i === layout.sections.length - 1 ? layout.more : undefined}
          title={section.title ? t(section.title) : undefined}
        />
      ))
    : null;
  const integrations = (only: Parameters<typeof IntegrationsSection>[0]['only']) => (
    <IntegrationsSection
      t={t}
      lang={lang}
      client={client}
      secretsFailed={data.secretsFailed}
      pinConfigured={data.pinConfigured}
      secrets={secrets}
      twitch={twitch}
      plugins={state.stream.session.plugins}
      onOpenRemote={() => navigate({ area: 'devices', tab: 'devices' })}
      homeTest={homeTest}
      only={only}
    />
  );

  const loading = !config && !data.configFailed && (
    <div aria-hidden="true">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="skeleton skeleton-row" />
      ))}
    </div>
  );

  let body: React.ReactNode;
  switch (page) {
    case 'privacy':
      body = (
        <>
          <section className="sec" aria-labelledby="h-set-privacy-level">
            <h2 id="h-set-privacy-level" className="sec-title">
              {t('privacy_mode')}
            </h2>
            <PrivacyChoice t={t} state={state} client={client} device={device} />
          </section>
          <section className="sec" aria-labelledby="h-set-mode">
            <h2 id="h-set-mode" className="sec-title">
              {t('mode_title')}
            </h2>
            <p className="lede">{t('ux_mode_lede')}</p>
            <ModeChoice t={t} state={state} client={client} device={device} />
          </section>
          <p className="hint">{captureLine(t, state.capture)}</p>
          {forms}
          <ConsentTile t={t} lang={lang} client={client} pinConfigured={data.pinConfigured} />
          <details className="more">
            <summary>{t('ux_set_telegram')}</summary>
            {integrations(['telegram'])}
          </details>
        </>
      );
      break;
    case 'voice':
      body = (
        <>
          <VoiceModelTile t={t} lang={lang} client={client} />
          {forms}
          <details className="more">
            <summary>{t('ux_set_voice_live')}</summary>
            <VoiceFacts t={t} state={state} />
          </details>
        </>
      );
      break;
    case 'stream':
      body = (
        <>
          <StreamSetup t={t} lang={lang} client={client} />
          {integrations(['twitch', 'obs'])}
          {forms}
        </>
      );
      break;
    case 'you':
      body = (
        <>
          {forms}
          <ProfileTile t={t} lang={lang} client={client} />
          {integrations(['calendar'])}
        </>
      );
      break;
    case 'nox':
      body = (
        <>
          {forms}
          <PersonalityTile
            t={t}
            client={client}
            personality={data.personality}
            failed={data.personalityFailed}
            onSaved={data.setPersonality}
          />
        </>
      );
      break;
    case 'home':
      body = (
        <>
          {integrations(['home'])}
          {forms}
        </>
      );
      break;
    case 'ai':
      body = (
        <>
          <ProvidersList
            t={t}
            lang={lang}
            state={state}
            failed={props.providersFailed}
            onRetry={props.onRefreshProviders}
            disabled={client === null}
          />
          {forms}
          <LocalAiTile t={t} lang={lang} client={client} />
        </>
      );
      break;
    case 'system': {
      const leftovers = config
        ? unplacedPaths(config.schema.filter((s) => s.type !== 'structured').map((s) => s.path))
        : [];
      body = (
        <>
          <HealthTable t={t} lang={lang} state={state} />
          <RemoteMaintenance t={t} lang={lang} client={client} />
          {forms}
          {config && leftovers.length > 0 && (
            <ConfigForm
              t={t}
              client={client}
              config={config}
              onApplied={data.applyValues}
              paths={[]}
              morePaths={leftovers}
            />
          )}
          <details className="more">
            <summary>{t('ux_set_system_history')}</summary>
            <HealthHistoryTable t={t} lang={lang} history={data.history} />
          </details>
          <p className="hint">{t('settings_file')}</p>
          <button type="button" className="btn btn--sm btn--plain" disabled={client === null} onClick={data.reload}>
            {t('settings_refresh')}
          </button>
        </>
      );
      break;
    }
  }

  return (
    <div className="page">
      <a
        className="back"
        href="#/settings"
        onClick={(e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
          e.preventDefault();
          navigate({ area: 'settings', page: null });
        }}
      >
        <Icon name="back" />
        {t('ux_area_settings')}
      </a>
      <header className="page-h">
        <h1>{t(layout.title)}</h1>
        <p>{t(layout.lede)}</p>
      </header>
      {page === 'system' && (
        <section className="sec" aria-labelledby="h-look">
          <h2 id="h-look" className="sec-title">
            {t('theme_label')}
          </h2>
          <div role="radiogroup" aria-labelledby="h-look" className="seg">
            {THEME_PREFS.map((pref) => (
              <button
                key={pref}
                type="button"
                role="radio"
                aria-checked={props.theme === pref}
                tabIndex={props.theme === pref ? 0 : -1}
                onClick={() => props.onTheme(pref)}
                onKeyDown={(e) => {
                  const i = THEME_PREFS.indexOf(pref);
                  const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
                  if (!step) return;
                  e.preventDefault();
                  const next = THEME_PREFS[(i + step + THEME_PREFS.length) % THEME_PREFS.length]!;
                  props.onTheme(next);
                  const group = e.currentTarget.parentElement;
                  window.setTimeout(() => group?.querySelector<HTMLElement>('[aria-checked="true"]')?.focus(), 0);
                }}
              >
                {t(THEME_LABEL[pref])}
              </button>
            ))}
          </div>
        </section>
      )}
      {data.configFailed && <p className="muted">{t('settings_unavailable')}</p>}
      {loading}
      <LocalOnly t={t} device={device} note="device_admin_note">
        {body}
      </LocalOnly>
    </div>
  );
}
