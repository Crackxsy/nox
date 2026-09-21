/**
 * The credentials a first-time user actually needs: Twitch, OBS, Telegram, Home Assistant.
 *
 * This section is first on the page. It used to sit roughly 5000 px below thirty-five config rows,
 * which meant the one part of Settings a stranger has to reach was the one part they could not find.
 *
 * Each tile carries its own state line, so a stored-but-wrong OBS password is distinguishable from
 * an OBS that is simply not running.
 */

import { type Lang, type T, twitchStateLabel } from '../../i18n';
import { type IpcClient } from '../../ipc';
import type { StreamPluginStatus } from '../../model';
import { Detail, StateWord, Tile, toneFor } from '../../ui';
import { SecretField } from './SecretField';
import type { HomeTestController } from './useHomeTest';
import type { SecretsController } from './useSecrets';
import type { TwitchFlow } from './useTwitchDeviceFlow';

export const TWITCH_CLIENT_ID = 'nox/twitch/client_id';
export const OBS_PASSWORD = 'nox/obs/websocket_password';
export const TELEGRAM_TOKEN = 'nox/telegram/bot_token';
export const HOME_ACCESS_TOKEN = 'nox/home/access_token';

export interface IntegrationsSectionProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  secretsFailed: boolean;
  pinConfigured: boolean;
  secrets: SecretsController;
  twitch: TwitchFlow;
  /** Live plugin connection state, so OBS and Telegram get the same honesty Twitch has. */
  plugins: StreamPluginStatus;
  /** Takes the user to the Remote page's pairing tile. */
  onOpenRemote: () => void;
  /** The Home Assistant connection test; it owns its own request and its own verdict. */
  homeTest: HomeTestController;
}

export function IntegrationsSection({
  t,
  client,
  secretsFailed,
  pinConfigured,
  secrets,
  twitch,
  plugins,
  onOpenRemote,
  homeTest,
}: IntegrationsSectionProps) {
  const disabled = client === null;
  const twitchState = twitch.status?.state ?? 'idle';

  const secretProps = (name: string) => ({
    t,
    name,
    present: secrets.present(name),
    editing: secrets.editing[name] === true,
    onEditing: (next: boolean) => secrets.setEditing(name, next),
    value: secrets.draft[name] ?? '',
    onValue: (value: string) => secrets.setDraft(name, value),
    busy: secrets.busy === name,
    disabled,
    // The PIN belongs to the credential being worked on: it appears as soon as this one is being
    // filled in (or has just been refused), and never under the two that are merely on screen.
    pinActive:
      secrets.pinFor === name ||
      secrets.editing[name] === true ||
      (secrets.draft[name] ?? '') !== '',
    pinConfigured,
    pin: secrets.pin,
    onPin: (value: string) => secrets.setPin(name, value),
    pinMessage: secrets.pinFor === name ? secrets.pinMessage : ('' as const),
    onSave: () => secrets.save(name),
    onDelete: () => secrets.remove(name),
  });

  return (
    <section aria-labelledby="h-integrations" id="settings-integrations">
      <div className="rail-head">
        <div>
          <h3 id="h-integrations" className="rail-title">
            {t('integrations_title')}
          </h3>
          <p className="rail-sub">{t('integrations_hint')}</p>
        </div>
      </div>

      {secrets.error && (
        <p role="alert" className="alert">
          {secrets.error}
        </p>
      )}

      {secretsFailed ? (
        <p className="muted">{t('secrets_unavailable')}</p>
      ) : (
        <>
          <div className="tiles tiles--single">
            <Tile
              feature
              level={4}
              id="twitch"
              eyebrow={twitchStateLabel(t, twitchState)}
              title={t('stream_plugin_twitch')}
              lede={secrets.present(TWITCH_CLIENT_ID) ? undefined : t('twitch_setup_title')}
            >
              {twitch.error && (
                <p role="alert" className="alert">
                  {twitch.error}
                </p>
              )}

              {!secrets.present(TWITCH_CLIENT_ID) ? (
                <>
                  <ol className="steps">
                    <li>{t('twitch_step_1')}</li>
                    <li>{t('twitch_step_2')}</li>
                    <li>{t('twitch_step_3')}</li>
                    <li>{t('twitch_step_4')}</li>
                  </ol>
                  <SecretField {...secretProps(TWITCH_CLIENT_ID)} labelKey="twitch_client_id_label" />
                </>
              ) : twitch.failed ? (
                <p className="muted">{t('secrets_unavailable')}</p>
              ) : twitchState === 'authorized' ? (
                <>
                  <dl className="facts">
                    <dt>{t('twitch_account')}</dt>
                    <dd>{twitch.status?.login || t('unknown')}</dd>
                  </dl>
                  <div className="tile-actions">
                    <button
                      type="button"
                      className="btn btn--quiet"
                      disabled={disabled || twitch.busy}
                      onClick={twitch.disconnect}
                    >
                      {t('twitch_disconnect')}
                    </button>
                  </div>
                </>
              ) : twitch.device ? (
                <div role="status" aria-live="polite">
                  <p className="label">{t('twitch_code_label')}</p>
                  <p className="code-display">{twitch.device.userCode}</p>
                  <p className="hint">{t('twitch_code_hint')}</p>
                  <div className="tile-actions">
                    <a
                      className="btn"
                      href={twitch.device.verificationUri}
                      target="_blank"
                      rel="noreferrer noopener"
                    >
                      {t('twitch_open_link')}
                    </a>
                  </div>
                </div>
              ) : (
                <>
                  {twitch.expired && <p className="hint">{t('twitch_code_expired')}</p>}
                  {twitch.status?.error && <p className="hint break">{twitch.status.error}</p>}
                  <div className="tile-actions">
                    <button
                      type="button"
                      className="btn"
                      disabled={disabled || twitch.busy}
                      onClick={twitch.connect}
                    >
                      {twitch.busy ? t('twitch_connecting') : t('twitch_connect')}
                    </button>
                  </div>
                </>
              )}
            </Tile>
          </div>

          <div className="tiles tiles--pair">
            <Tile level={4} id="obs" title={t('stream_plugin_obs')}>
              <StateWord tone={toneFor(plugins.obs)} label={obsLabel(t, plugins.obs)} />
              <SecretField
                {...secretProps(OBS_PASSWORD)}
                labelKey="obs_password_label"
                hintKey="obs_hint"
              />
            </Tile>

            <Tile level={4} id="home" title={t('settings_group_home')}>
              <ol className="steps">
                <li>{t('home_step_1')}</li>
                <li>{t('home_step_2')}</li>
                <li>{t('home_step_3')}</li>
              </ol>
              <SecretField
                {...secretProps(HOME_ACCESS_TOKEN)}
                labelKey="home_token_label"
                hintKey="home_token_hint"
              />
              {homeTest.error && (
                <p role="alert" className="alert">
                  {homeTest.error}
                </p>
              )}
              <div className="tile-actions">
                <button
                  type="button"
                  className="btn btn--sm"
                  disabled={disabled || homeTest.busy}
                  onClick={homeTest.test}
                >
                  {homeTest.busy ? t('home_test_running') : t('home_test_button')}
                </button>
              </div>
              <p role="status" aria-live="polite" className="hint break">
                {homeTest.message && <Detail label={homeTest.message} detail={homeTest.detail} />}
              </p>
            </Tile>

            <Tile level={4} id="telegram" title="Telegram">
              <SecretField
                {...secretProps(TELEGRAM_TOKEN)}
                labelKey="telegram_token_label"
                hintKey="telegram_hint"
              />
              <div className="tile-actions">
                <button type="button" className="link" onClick={onOpenRemote}>
                  {t('telegram_pair_link')}
                </button>
              </div>
            </Tile>
          </div>
        </>
      )}
    </section>
  );
}

/** "verbunden" only when the plugin really is; everything else reads as "nicht verbunden". */
function obsLabel(t: T, status: string): string {
  return status === 'connected' ? t('plugin_connected') : t('obs_state_unknown');
}
