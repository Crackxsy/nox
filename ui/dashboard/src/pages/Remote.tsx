/**
 * Remote page (EPIC-17, Spec v0.8 §8): pair a phone, see which devices are paired, revoke one.
 *
 * Three honesty rules, same as everywhere else in this dashboard:
 * - the one-time code is shown exactly as the core returned it and never re-requested or cached;
 *   it is not stored anywhere, so leaving the page loses it and a new one must be generated;
 * - no key material is ever rendered — the core's `RemoteDevice` model has no such field;
 * - when the feature is off the page says so *and disables the form*. `remote.enabled` is false by
 *   default, and the core then never installs the `remote.*` requests at all, so the honest answer
 *   is "not found", not a red error banner over a form that still looks usable.
 */

import { useState } from 'react';

import { formatTimestamp } from '../../../shared/format';
import { useIpcAction, useRefreshOnConnect } from '../hooks';
import type { Lang, T } from '../i18n';
import { type IpcClient, api } from '../ipc';
import { type RemoteDevice, type RemotePairCode, parsePairCode, parseRemoteDevices } from '../model';
import { Hero, StateWord, Tile } from '../ui';

export interface RemotePageProps {
  t: T;
  lang: Lang;
  /** null while offline: every request would fail, so the controls are disabled instead. */
  client: IpcClient | null;
  /** Takes the user to the `remote.enabled` switch on the Settings tab. */
  onOpenSettings: () => void;
}

export function RemotePage({ t, lang, client, onOpenSettings }: RemotePageProps) {
  const [devices, setDevices] = useState<RemoteDevice[]>([]);
  /** `null` only until the first answer; a failed request means "off", never "unknown forever". */
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [code, setCode] = useState<RemotePairCode | null>(null);
  const [name, setName] = useState('');
  const { busy, error, setError, run } = useIpcAction(client, t);

  const refresh = async (c: IpcClient, cancelled: () => boolean = () => false) => {
    try {
      const parsed = parseRemoteDevices(await api.remoteDevices(c));
      if (cancelled()) return;
      setDevices(parsed?.devices ?? []);
      setEnabled(parsed?.enabled === true);
      setError('');
    } catch {
      // Unknown request, refused, or `remote.enabled: false` — from this page they are one fact:
      // there is nothing to pair. Saying that is more use than repeating the core's wording.
      if (cancelled()) return;
      setDevices([]);
      setEnabled(false);
      setError('');
    }
  };

  useRefreshOnConnect(client, refresh);

  const pair = () =>
    run('pair', async (c) => {
      setCode(parsePairCode(await api.remotePairStart(c, name)));
      await refresh(c);
    });

  const revoke = (deviceId: string) =>
    run(deviceId, async (c) => {
      await api.remoteUnpair(c, deviceId);
      await refresh(c);
    });

  const off = enabled === false;
  const disabled = client === null || off;

  return (
    <div className="page wrap">
      <Hero
        title={t('hero_remote')}
        sub={t('hero_remote_sub')}
        links={
          <a className="link" href="#pair">
            {t('jump_pair')}
          </a>
        }
      />

      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      <div className="tiles tiles--single">
        <Tile
          feature
          id="pair"
          eyebrow={off ? t('remote_unavailable') : undefined}
          title={t('remote_tile_title')}
          lede={off ? t('remote_disabled') : t('remote_hint')}
        >
          {off ? (
            <div className="tile-actions">
              <button type="button" className="link" onClick={onOpenSettings}>
                {t('remote_settings_link')}
              </button>
            </div>
          ) : (
            <>
              <div className="row-controls">
                <div className="field field--grow">
                  <label htmlFor="remote-name" className="label">
                    {t('remote_pair_name')}
                  </label>
                  <input
                    id="remote-name"
                    className="input"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    maxLength={64}
                  />
                </div>
                <button
                  type="button"
                  className="btn"
                  disabled={disabled || busy === 'pair'}
                  onClick={() => void pair()}
                >
                  {t('remote_pair_button')}
                </button>
              </div>

              {code && (
                <div role="status" aria-live="polite" className="field field--spaced">
                  <p className="label">{t('remote_code_label')}</p>
                  <p className="code-display">{code.code}</p>
                  <p className="hint">{t('remote_code_hint')}</p>
                  <p className="hint">
                    {t('remote_expires_at')}:{' '}
                    <span title={code.expiresAt}>{formatTimestamp(code.expiresAt, lang)}</span>
                  </p>
                </div>
              )}

              <div className="tile-actions">
                <button type="button" className="link" onClick={onOpenSettings}>
                  {t('remote_telegram_link')}
                </button>
              </div>
            </>
          )}
        </Tile>
      </div>

      <section aria-labelledby="h-remote-devices">
        <div className="rail-head">
          <div>
            <h3 id="h-remote-devices" className="rail-title">
              {t('remote_title')}
            </h3>
            {devices.length === 0 && <p className="rail-sub">{t('remote_empty')}</p>}
          </div>
        </div>
        {devices.length > 0 && (
          <div className="table-scroll">
            <table className="specs">
              <caption className="sr-only">{t('remote_title')}</caption>
              <thead>
                <tr>
                  <th scope="col">{t('remote_col_name')}</th>
                  <th scope="col">{t('remote_col_state')}</th>
                  <th scope="col">{t('remote_col_paired')}</th>
                  <th scope="col">{t('remote_col_last_seen')}</th>
                  <th scope="col" aria-label={t('remote_revoke')} />
                </tr>
              </thead>
              <tbody>
                {devices.map((device) => {
                  const revoked = device.revokedAt !== null;
                  return (
                    <tr key={device.id}>
                      <th scope="row">{device.name || t('unknown')}</th>
                      <td>
                        <StateWord
                          tone={revoked ? 'off' : 'ok'}
                          label={revoked ? t('remote_state_revoked') : t('remote_state_active')}
                        />
                      </td>
                      <td className="muted nowrap" title={device.pairedAt}>
                        {formatTimestamp(device.pairedAt, lang)}
                      </td>
                      <td className="muted nowrap" title={device.lastSeenAt ?? undefined}>
                        {device.lastSeenAt === null
                          ? t('remote_never')
                          : formatTimestamp(device.lastSeenAt, lang)}
                      </td>
                      <td>
                        {!revoked && (
                          <button
                            type="button"
                            className="link link--plain"
                            disabled={disabled || busy === device.id}
                            onClick={() => void revoke(device.id)}
                          >
                            {t('remote_revoke')}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
