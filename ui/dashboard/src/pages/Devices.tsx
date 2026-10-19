/**
 * Devices in the home network: pair a new one, see who is paired, remove one, and - on a paired
 * device - make the browser warning go away for good.
 *
 * Pairing is one tile: a button, then a QR code to scan with the new device's camera, the same
 * link to copy, the code to type if it comes to that, and until when it is valid. The code is
 * shown exactly as the core issued it and never cached; leaving the page loses it.
 *
 * Honest about the state: when network access is off the section says so and points at the one
 * setting that turns it on; while it starts, or when it failed, it says that instead of showing a
 * form that cannot work.
 */

import { useCallback, useEffect, useState } from 'react';

import { formatTimestamp } from '../../../shared/format';
import { useIpcAction, useRefreshOnConnect } from '../hooks';
import { fill, type Lang, type T } from '../i18n';
import { type IpcClient, api } from '../ipc';
import {
  type LanDevice,
  type LanPairing,
  type LanStatus,
  groupCode,
  parseLanDevices,
  parseLanPairing,
  parseLanStatus,
  shortFingerprint,
} from '../model';
import { StateWord, Tile } from '../ui';

export interface DevicesSectionProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  /** This dashboard runs on a paired device (not on the machine Nox runs on). */
  device: boolean;
  /** Bumped by the app on `remote.paired` / `remote.revoked`; reloads the list. */
  revision: number;
  /** The name of the device paired last while this page was open, for the success line. */
  justPaired: string;
  onOpenSettings: () => void;
  /** This device signed itself out: the app forgets its credentials. */
  onSignedOut: () => void;
}

interface InstallPromptEvent extends Event {
  prompt: () => Promise<void>;
}

/** The browser's own "install this app" offer, when it makes one (Chromium, on HTTPS). */
function useInstallPrompt(): (() => void) | null {
  const [event, setEvent] = useState<InstallPromptEvent | null>(null);
  useEffect(() => {
    const onPrompt = (e: Event) => {
      e.preventDefault();
      setEvent(e as InstallPromptEvent);
    };
    window.addEventListener('beforeinstallprompt', onPrompt);
    return () => window.removeEventListener('beforeinstallprompt', onPrompt);
  }, []);
  return event
    ? () => {
        void event.prompt();
        setEvent(null);
      }
    : null;
}

export function DevicesSection({
  t,
  lang,
  client,
  device,
  revision,
  justPaired,
  onOpenSettings,
  onSignedOut,
}: DevicesSectionProps) {
  const [status, setStatus] = useState<LanStatus | null>(null);
  const [devices, setDevices] = useState<LanDevice[]>([]);
  const [pairing, setPairing] = useState<LanPairing | null>(null);
  /** The pairing revision a code was made at: a device paired after it closes the panel. */
  const [pairingRevision, setPairingRevision] = useState(0);
  const [name, setName] = useState('');
  const [pin, setPin] = useState('');
  const [copied, setCopied] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  /** The device whose remove button was pressed once; a second press removes it. */
  const [armed, setArmed] = useState<string | null>(null);
  const { busy, error, setError, run } = useIpcAction(client, t);
  const install = useInstallPrompt();

  const load = useCallback(
    async (c: IpcClient, cancelled: () => boolean = () => false) => {
      try {
        const [s, d] = await Promise.all([api.lanStatus(c), api.lanDevices(c)]);
        if (cancelled()) return;
        setStatus(parseLanStatus(s));
        setDevices(parseLanDevices(d) ?? []);
        setError('');
      } catch {
        // An older core without network access: the same thing as "off" for this page.
        if (cancelled()) return;
        setStatus({ enabled: false, running: false, error: '', urls: [], fingerprint: '' });
        setDevices([]);
      }
    },
    [setError],
  );

  useRefreshOnConnect(client, load);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() sets state only after awaiting the core; the rule does not see past the await
    if (client && revision > 0) void load(client);
  }, [client, revision, load]);

  // The expiry is shown as a time; this only decides when to say "expired" instead.
  useEffect(() => {
    if (!pairing) return;
    const timer = window.setInterval(() => setNow(Date.now()), 5000);
    return () => window.clearInterval(timer);
  }, [pairing]);

  const pair = () =>
    run('pair', async (c) => {
      const result = parseLanPairing(await api.lanPairStart(c, name, pin));
      setPairing(result);
      setPairingRevision(revision);
      setCopied(false);
      setNow(Date.now());
      setPin('');
    });

  // Disarm after a few seconds, so a stray second tap much later removes nothing.
  useEffect(() => {
    if (armed === null) return;
    const timer = window.setTimeout(() => setArmed(null), 6000);
    return () => window.clearTimeout(timer);
  }, [armed]);

  const remove = (target: LanDevice) => {
    const own = target.current;
    if (armed !== target.id) {
      setArmed(target.id);
      return;
    }
    setArmed(null);
    void run(target.id, async (c) => {
      await api.lanUnpair(c, target.id, pin);
      setPin('');
      if (own) onSignedOut();
      else await load(c);
    });
  };

  const copy = async (url: string) => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  if (status === null) return null;

  if (!status.enabled) {
    return (
      <div className="tiles tiles--single">
        <Tile feature id="lan" title={t('lan_off_title')} lede={t('lan_off_body')}>
          {!device && (
            <div className="tile-actions">
              <button type="button" className="link" onClick={onOpenSettings}>
                {t('lan_off_settings')}
              </button>
            </div>
          )}
        </Tile>
      </div>
    );
  }

  // A device paired since the code was made: the panel's job is done.
  const shown = pairing !== null && !(justPaired && revision > pairingRevision) ? pairing : null;
  const expired = shown !== null && Date.parse(shown.expiresAt) <= now;
  const notRunning = !status.running;

  return (
    <>
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}
      {justPaired && (
        <p role="status" className="banner banner--ok">
          {fill(t('lan_pair_done'), justPaired)}
        </p>
      )}

      <div className="tiles tiles--single">
        <Tile
          feature
          id="lan"
          eyebrow={notRunning ? (status.error ? t('remote_unavailable') : t('lan_starting')) : undefined}
          title={t('lan_pair_title')}
          lede={notRunning && status.error ? fill(t('lan_failed'), status.error) : t('lan_pair_lede')}
        >
          {!shown || expired ? (
            <div className="row-controls">
              <div className="field field--grow">
                <label htmlFor="lan-name" className="label">
                  {t('lan_pair_name')}
                </label>
                <input
                  id="lan-name"
                  className="input"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  maxLength={64}
                  autoComplete="off"
                />
              </div>
              {device && (
                <div className="field">
                  <label htmlFor="lan-pin" className="label">
                    {t('lan_pair_pin')}
                  </label>
                  <input
                    id="lan-pin"
                    className="input input--pin"
                    type="password"
                    inputMode="numeric"
                    autoComplete="off"
                    value={pin}
                    onChange={(e) => setPin(e.target.value)}
                    aria-describedby="lan-pin-hint"
                  />
                </div>
              )}
              <button
                type="button"
                className="btn"
                disabled={!client || notRunning || busy === 'pair'}
                onClick={() => void pair()}
              >
                {expired ? t('lan_pair_again') : t('lan_pair_button')}
              </button>
              {device && (
                <p id="lan-pin-hint" className="hint field--full">
                  {t('lan_pair_pin_hint')}
                </p>
              )}
              {expired && (
                <p role="status" className="hint field--full">
                  {t('lan_pair_expired')}
                </p>
              )}
            </div>
          ) : (
            <div className="pairing" role="region" aria-live="polite" aria-label={t('lan_pair_title')}>
              {shown.qr && <img className="pairing-qr" src={shown.qr} alt={t('lan_pair_qr_alt')} />}
              <div className="pairing-facts">
                <p className="label">{t('lan_pair_link')}</p>
                <p className="pairing-url">
                  <a href={shown.url} className="link" rel="noreferrer">
                    {shown.url}
                  </a>
                </p>
                <button type="button" className="btn btn--quiet btn--sm" onClick={() => void copy(shown.url)}>
                  {copied ? t('lan_pair_copied') : t('lan_pair_copy')}
                </button>
                <p className="label">{t('lan_pair_code')}</p>
                <p className="code-display">{groupCode(shown.code)}</p>
                <p className="hint">
                  {fill(t('lan_pair_valid_until'), formatTimestamp(shown.expiresAt, lang))}
                </p>
                <p className="label">{t('lan_fingerprint')}</p>
                <p className="fingerprint">{shortFingerprint(shown.fingerprint)}</p>
                <p className="hint">{t('lan_fingerprint_hint')}</p>
              </div>
            </div>
          )}
        </Tile>
      </div>

      <section aria-labelledby="h-lan-devices">
        <div className="rail-head">
          <div>
            <h3 id="h-lan-devices" className="rail-title">
              {t('lan_devices_title')}
            </h3>
            {devices.length === 0 && <p className="rail-sub">{t('lan_devices_empty')}</p>}
          </div>
        </div>
        {devices.length > 0 && (
          <ul className="device-list" aria-labelledby="h-lan-devices">
            {devices.map((d) => {
              const revoked = d.revokedAt !== null;
              return (
                <li key={d.id} className={revoked ? 'device device--revoked' : 'device'}>
                  <div className="device-main">
                    <p className="device-name">
                      {d.name || t('unknown')}
                      {d.current && <span className="badge">{t('lan_this_device')}</span>}
                    </p>
                    <p className="device-meta">
                      {t('remote_col_paired')}: {formatTimestamp(d.pairedAt, lang)}
                      {' · '}
                      {t('remote_col_last_seen')}:{' '}
                      {d.lastSeenAt === null ? t('remote_never') : formatTimestamp(d.lastSeenAt, lang)}
                    </p>
                  </div>
                  <StateWord
                    tone={revoked ? 'off' : 'ok'}
                    label={revoked ? t('remote_state_revoked') : t('remote_state_active')}
                  />
                  {!revoked && (d.current || !device) && (
                    <div className="device-action">
                      <button
                        type="button"
                        className={armed === d.id ? 'btn btn--sm btn--armed' : 'link link--plain'}
                        disabled={!client || busy === d.id}
                        onClick={() => remove(d)}
                        aria-describedby={armed === d.id ? `confirm-${d.id}` : undefined}
                      >
                        {d.current ? t('lan_sign_out') : t('lan_remove')}
                      </button>
                      {armed === d.id && (
                        <p id={`confirm-${d.id}`} role="status" className="hint">
                          {d.current ? t('lan_sign_out_confirm') : fill(t('lan_remove_confirm'), d.name)}
                        </p>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {device && (
        <section aria-labelledby="h-lan-ca" className="tiles tiles--single">
          <Tile id="lan-ca" title={t('lan_ca_title')} lede={t('lan_ca_lede')}>
            <div className="tile-actions">
              <a className="btn" href="/nox-ca.crt" download="nox-heimnetz.crt">
                {t('lan_ca_download')}
              </a>
              {install && (
                <button type="button" className="btn btn--quiet" onClick={install}>
                  {t('lan_install_app')}
                </button>
              )}
            </div>
            <details className="ca-howto">
              <summary>{t('lan_ca_how')}</summary>
              <ul>
                <li>{t('lan_ca_android')}</li>
                <li>{t('lan_ca_ios')}</li>
                <li>{t('lan_ca_windows')}</li>
                <li>{t('lan_ca_mac')}</li>
              </ul>
            </details>
            {status.fingerprint && (
              <p className="hint">
                {t('lan_fingerprint')}: <span className="fingerprint">{shortFingerprint(status.fingerprint)}</span>
              </p>
            )}
          </Tile>
        </section>
      )}
    </>
  );
}
