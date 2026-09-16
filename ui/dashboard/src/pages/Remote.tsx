/**
 * Remote page (EPIC-17, Spec v0.8 §8): pair a phone, see which devices are paired, revoke one.
 *
 * Three honesty rules, same as everywhere else in this dashboard:
 * - the one-time code is shown exactly as the core returned it and never re-requested or cached;
 *   it is not stored anywhere, so leaving the page loses it and a new one must be generated;
 * - no key material is ever rendered — the core's `RemoteDevice` model has no such field;
 * - when `remote.enabled` is false the core answers `unavailable`, and the page says so instead of
 *   pretending the buttons would work.
 */

import { useCallback, useEffect, useState } from 'react';

import type { RemoteDevice, RemotePairCode } from '../../../shared/generated/ipc';
import type { T } from '../i18n';
import { type IpcClient, api } from '../ipc';
import { Hero, StateWord, Tile } from '../ui';

export interface RemotePageProps {
  t: T;
  /** null while offline: every request would fail, so the controls are disabled instead. */
  client: IpcClient | null;
}

export function RemotePage({ t, client }: RemotePageProps) {
  const [devices, setDevices] = useState<RemoteDevice[]>([]);
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [code, setCode] = useState<RemotePairCode | null>(null);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  const disabled = client === null;

  const refresh = useCallback(
    async (c: IpcClient) => {
      try {
        const payload = await api.remoteDevices(c);
        setDevices(
          Array.isArray(payload.devices) ? (payload.devices as unknown as RemoteDevice[]) : [],
        );
        setEnabled(payload.enabled === true);
      } catch (e) {
        setError(`${t('error_prefix')}: ${e instanceof Error ? e.message : String(e)}`);
      }
    },
    [t],
  );

  useEffect(() => {
    if (client) void refresh(client);
  }, [client, refresh]);

  const pair = async () => {
    if (!client) return;
    setBusy('pair');
    setError('');
    try {
      setCode((await api.remotePairStart(client, name)) as unknown as RemotePairCode);
      await refresh(client);
    } catch (e) {
      setError(`${t('error_prefix')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const revoke = async (deviceId: string) => {
    if (!client) return;
    setBusy(deviceId);
    setError('');
    try {
      await api.remoteUnpair(client, deviceId);
      await refresh(client);
    } catch (e) {
      setError(`${t('error_prefix')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

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
          eyebrow={enabled === false ? t('remote_disabled') : t('tab_remote')}
          title={t('remote_pair_button')}
          lede={t('remote_hint')}
        >
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
              disabled={disabled || enabled === false || busy === 'pair'}
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
                {t('remote_expires_at')}: {code.expires_at}
              </p>
            </div>
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
                  const revoked = Boolean(device.revoked_at);
                  return (
                    <tr key={device.id}>
                      <th scope="row">{device.name || t('unknown')}</th>
                      <td>
                        <StateWord
                          status={revoked ? 'off' : 'available'}
                          label={revoked ? t('remote_state_revoked') : t('remote_state_active')}
                        />
                      </td>
                      <td className="muted nowrap">{device.paired_at}</td>
                      <td className="muted nowrap">{device.last_seen_at ?? t('remote_never')}</td>
                      <td>
                        {!revoked && (
                          <button
                            type="button"
                            className="link"
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
