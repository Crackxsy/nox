/**
 * Devices in the home network: pair a new one, see who is paired, remove one, and - on a paired
 * device - make the browser warning go away for good.
 *
 * Pairing is one tile: a button, then a QR code to scan with the new device's camera, the same
 * link to copy, the code to type if it comes to that, and until when it is valid. The code is
 * shown exactly as the core issued it and never cached; leaving the page loses it.
 *
 * Honest about the state, one at a time: loading; network access off (also what an older core
 * that does not know `lan.status` at all means, and only that); an answer this page cannot read;
 * a request that failed; starting; not running. None of them shows a form that cannot work - and
 * a paired device, which needs the security PIN to pair or remove others, is told where to do it
 * when no PIN is set. Refusals are the core's reasons in words, never its English text.
 *
 * The list stays current by itself: on `remote.paired` / `remote.revoked`, when the page becomes
 * visible again, and every 30 seconds while it is.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { failureKind } from '../../../shared/errors';
import { formatTimestamp } from '../../../shared/format';
import { refusalText, useIpcAction, useRefreshOnConnect, useWhileVisible } from '../hooks';
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
  spellCode,
} from '../model';
import { type PlatformInput, currentPlatform, isIosBrowserTab } from '../platform';
import { StateWord, Tile } from '../ui';

/** A device that paired while this dashboard was open (`remote.paired`); `seq` makes each new. */
export interface PairedNotice {
  name: string;
  seq: number;
}

export interface DevicesSectionProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  /** This dashboard runs on a paired device (not on the machine Nox runs on). */
  device: boolean;
  /** Bumped by the app on `remote.paired` / `remote.revoked`; reloads the list. */
  revision: number;
  /** The device paired last while this page was open, for the success line. */
  justPaired: PairedNotice | null;
  onOpenSettings: () => void;
  /** This device signed itself out: the app forgets its credentials. */
  onSignedOut: () => void;
  /** Injected in tests; read from the browser otherwise. */
  platform?: PlatformInput;
}

/** How long a "Remove" stays armed; typing the PIN restarts it. */
export const DISARM_MS = 12000;
/** How long "iPhone is paired." stays. */
export const PAIRED_BANNER_MS = 8000;
/** How often the list is refreshed while the page is visible. */
export const REFRESH_MS = 30000;

type View =
  | { kind: 'loading' }
  /** An older core without network access: the same thing as "off" for this page. */
  | { kind: 'unsupported' }
  | { kind: 'unreadable' }
  | { kind: 'failed'; message: string }
  | { kind: 'ready'; status: LanStatus; devices: LanDevice[] };

type CopyState = 'idle' | 'copied' | 'failed';

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

/** Both labels in one grid cell: the button is as wide as the longer one, so arming moves nothing. */
function SwapLabel({ shown, label, alternate }: { shown: boolean; label: string; alternate: string }) {
  return (
    <span className="swap">
      <span className={shown ? 'swap-hidden' : undefined} aria-hidden={shown || undefined}>
        {label}
      </span>
      <span className={shown ? undefined : 'swap-hidden'} aria-hidden={!shown || undefined}>
        {alternate}
      </span>
    </span>
  );
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
  platform,
}: DevicesSectionProps) {
  const [view, setView] = useState<View>({ kind: 'loading' });
  const [pairing, setPairing] = useState<LanPairing | null>(null);
  const [name, setName] = useState('');
  const [pin, setPin] = useState('');
  const [copy, setCopy] = useState<CopyState>('idle');
  const [now, setNow] = useState(() => Date.now());
  /** The device whose remove button was pressed once; a second press removes it. */
  const [armed, setArmed] = useState<string | null>(null);
  const [removePin, setRemovePin] = useState('');
  const [banner, setBanner] = useState('');
  const [seenPaired, setSeenPaired] = useState(justPaired?.seq ?? 0);
  const [ios] = useState(() => isIosBrowserTab(platform ?? currentPlatform()));
  /** Whether the request in flight carried a PIN: decides "PIN required" vs "PIN wrong". */
  const pinSentRef = useRef(false);
  const removePinRef = useRef<HTMLInputElement | null>(null);
  const describe = useCallback((e: unknown) => refusalText(t, e, pinSentRef.current), [t]);
  const { busy, error, setError, run } = useIpcAction(client, t, describe);
  const install = useInstallPrompt();

  /** `background`: a refresh of a page already shown keeps what it shows when the refresh fails. */
  const load = useCallback(
    async (c: IpcClient, cancelled: () => boolean = () => false, background = false) => {
      try {
        const [s, d] = await Promise.all([api.lanStatus(c), api.lanDevices(c)]);
        if (cancelled()) return;
        const status = parseLanStatus(s);
        const devices = parseLanDevices(d);
        setView(status && devices ? { kind: 'ready', status, devices } : { kind: 'unreadable' });
      } catch (e) {
        if (cancelled()) return;
        if (failureKind(e) === 'unknown_request') {
          setView({ kind: 'unsupported' });
          return;
        }
        if (background) return;
        setView({ kind: 'failed', message: refusalText(t, e, false, '') });
      }
    },
    [t],
  );

  useRefreshOnConnect(client, load);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- load() sets state only after awaiting the core; the rule does not see past the await
    if (client && revision > 0) void load(client, () => false, true);
  }, [client, revision, load]);

  useWhileVisible(client !== null && view.kind === 'ready', REFRESH_MS, () => {
    if (client) void load(client, () => false, true);
  });

  // A device paired since the code was made: the panel's job is done, the name was used, and a
  // short line says who arrived.
  if (justPaired && justPaired.seq !== seenPaired) {
    setSeenPaired(justPaired.seq);
    setBanner(justPaired.name);
    if (pairing) {
      setPairing(null);
      setName('');
      setCopy('idle');
    }
  }
  useEffect(() => {
    if (!banner) return;
    const timer = window.setTimeout(() => setBanner(''), PAIRED_BANNER_MS);
    return () => window.clearTimeout(timer);
  }, [banner, seenPaired]);

  // The expiry is shown as a time; this only decides when to say "expired" instead.
  useEffect(() => {
    if (!pairing) return;
    const timer = window.setInterval(() => setNow(Date.now()), 5000);
    return () => window.clearInterval(timer);
  }, [pairing]);

  // Disarm after a while, so a stray second tap much later removes nothing.
  useEffect(() => {
    if (armed === null) return;
    const timer = window.setTimeout(() => setArmed(null), DISARM_MS);
    return () => window.clearTimeout(timer);
  }, [armed, removePin]);

  const pair = () => {
    pinSentRef.current = pin.trim() !== '';
    return run('pair', async (c) => {
      let payload: Record<string, unknown>;
      try {
        payload = await api.lanPairStart(c, name, pin.trim());
      } catch (e) {
        // Refused: perhaps a PIN was set since this page last looked. Look again, so the field
        // for it is there when the person reads why it did not work.
        if (failureKind(e) === 'refused') void load(c, () => false, true);
        throw e;
      }
      const result = parseLanPairing(payload);
      if (!result) {
        setError(t('lan_unreadable'));
        return;
      }
      setPairing(result);
      setCopy('idle');
      setNow(Date.now());
      setPin('');
    });
  };

  const remove = (target: LanDevice) => {
    if (armed !== target.id) {
      setArmed(target.id);
      setRemovePin('');
      setError('');
      return;
    }
    const withPin = device && !target.current;
    if (withPin && !removePin.trim()) {
      setError(t('reason_pin_required'));
      removePinRef.current?.focus();
      return;
    }
    setArmed(null);
    pinSentRef.current = withPin;
    void run(target.id, async (c) => {
      await api.lanUnpair(c, target.id, withPin ? removePin.trim() : '');
      setRemovePin('');
      if (target.current) onSignedOut();
      else await load(c);
    });
  };

  const copyLink = async (url: string) => {
    try {
      await navigator.clipboard.writeText(url);
      setCopy('copied');
    } catch {
      setCopy('failed');
    }
  };

  if (view.kind === 'loading') {
    return client ? (
      <p className="hint" role="status">
        {t('lan_loading')}
      </p>
    ) : null;
  }

  if (view.kind === 'unreadable' || view.kind === 'failed') {
    const lede = view.kind === 'unreadable' ? t('lan_unreadable') : view.message || undefined;
    return (
      <div className="tiles tiles--single">
        <Tile feature id="lan" title={t('lan_load_failed')} lede={lede}>
          <div className="tile-actions">
            <button
              type="button"
              className="link"
              disabled={!client}
              onClick={() => {
                if (!client) return;
                setView({ kind: 'loading' });
                void load(client);
              }}
            >
              {t('lan_retry')}
            </button>
          </div>
        </Tile>
      </div>
    );
  }

  if (view.kind === 'unsupported' || !view.status.enabled) {
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

  const { status, devices } = view;
  const expired = pairing !== null && Date.parse(pairing.expiresAt) <= now;
  const shown = pairing !== null && !expired ? pairing : null;
  const notRunning = !status.running;
  /** A paired device pairs only with the PIN, which needs one to be set. */
  const canPair = !device || status.pinSet;
  const technical = !device && (status.error || status.warning);
  const lede = notRunning ? (status.error ? t('lan_failed') : t('lan_starting')) : t('lan_pair_lede');
  const copyLine =
    copy === 'copied' ? t('lan_pair_copied') : copy === 'failed' ? t('lan_pair_copy_failed') : '';
  // One small live line for the pairing tile, always in the page so a screen reader hears it
  // change: the code is ready, copying worked or did not, the code expired.
  const pairingStatus = shown
    ? copyLine || fill(t('lan_pair_ready'), formatTimestamp(shown.expiresAt, lang))
    : expired && canPair
      ? t('lan_pair_expired')
      : '';
  const statusVisible = shown ? copyLine !== '' : pairingStatus !== '';

  return (
    <>
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}
      <p role="status" className={banner ? 'banner banner--ok' : 'sr-only'}>
        {banner ? fill(t('lan_pair_done'), banner) : ''}
      </p>

      <div className="tiles tiles--single">
        <Tile feature id="lan" title={t('lan_pair_title')} lede={lede}>
          {technical && (
            <details className="tile-technical">
              <summary>{t('lan_technical')}</summary>
              <p className="mono break">{status.error || status.warning}</p>
            </details>
          )}
          {!canPair ? (
            <p className="hint">{t('lan_pair_only_at_computer')}</p>
          ) : !shown ? (
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
              {status.pinSet && (
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
              {status.pinSet && (
                <p id="lan-pin-hint" className="hint field--full">
                  {t('lan_pair_pin_hint')}
                </p>
              )}
            </div>
          ) : (
            <div className="pairing">
              {shown.qr && <img className="pairing-qr" src={shown.qr} alt={t('lan_pair_qr_alt')} />}
              <div className="pairing-facts">
                <p className="label">{t('lan_pair_link')}</p>
                <p className="pairing-url">
                  <a href={shown.url} className="link link--plain" rel="noreferrer">
                    {shown.url}
                  </a>
                </p>
                <div className="pairing-buttons">
                  <button type="button" className="btn btn--quiet btn--sm" onClick={() => void copyLink(shown.url)}>
                    {t('lan_pair_copy')}
                  </button>
                  <button
                    type="button"
                    className="btn btn--quiet btn--sm"
                    onClick={() => {
                      setPairing(null);
                      setCopy('idle');
                    }}
                  >
                    {t('lan_pair_discard')}
                  </button>
                </div>
                <p className="label">{t('lan_pair_code')}</p>
                <p className="code-display">
                  <span className="code-display-value" aria-hidden="true">
                    {groupCode(shown.code)}
                  </span>
                  <span className="sr-only">{fill(t('lan_pair_code_spelled'), spellCode(shown.code))}</span>
                </p>
                <p className="hint">{fill(t('lan_pair_valid_until'), formatTimestamp(shown.expiresAt, lang))}</p>
                {shown.fingerprint && (
                  <>
                    <p className="label">{t('lan_fingerprint')}</p>
                    <p className="fingerprint">{shortFingerprint(shown.fingerprint)}</p>
                    <p className="hint">{t('lan_fingerprint_hint')}</p>
                  </>
                )}
              </div>
            </div>
          )}
          <p
            role="status"
            className={
              statusVisible
                ? copy === 'failed'
                  ? 'hint pairing-status pairing-status--warn'
                  : 'hint pairing-status'
                : 'sr-only'
            }
          >
            {pairingStatus}
          </p>
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
              const removed = d.revokedAt !== null;
              const canRemove = !removed && (d.current || !device || status.pinSet);
              const isArmed = armed === d.id;
              const withPin = device && !d.current;
              return (
                <li key={d.id} className={removed ? 'device device--revoked' : 'device'}>
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
                    tone={removed ? 'off' : 'ok'}
                    label={removed ? t('lan_state_removed') : t('lan_state_active')}
                  />
                  {canRemove && (
                    <div className="device-action">
                      <button
                        type="button"
                        className={isArmed ? 'btn btn--sm btn--armed' : 'btn btn--sm btn--quiet'}
                        disabled={!client || busy === d.id}
                        onClick={() => remove(d)}
                        aria-describedby={isArmed ? `confirm-${d.id}` : undefined}
                      >
                        <SwapLabel
                          shown={isArmed}
                          label={d.current ? t('lan_sign_out') : t('lan_remove')}
                          alternate={d.current ? t('lan_sign_out_armed') : t('lan_remove_armed')}
                        />
                      </button>
                    </div>
                  )}
                  {isArmed && (
                    <div className="device-confirm">
                      <p id={`confirm-${d.id}`} role="status" className="hint">
                        {d.current ? t('lan_sign_out_confirm') : fill(t('lan_remove_confirm'), d.name || t('unknown'))}
                      </p>
                      {withPin && (
                        <div className="field">
                          <label htmlFor={`remove-pin-${d.id}`} className="label">
                            {fill(t('lan_remove_pin'), d.name || t('unknown'))}
                          </label>
                          <input
                            ref={removePinRef}
                            id={`remove-pin-${d.id}`}
                            className="input input--pin"
                            type="password"
                            inputMode="numeric"
                            autoComplete="off"
                            value={removePin}
                            onChange={(e) => setRemovePin(e.target.value)}
                          />
                        </div>
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
            {ios && (
              <div className="ca-order">
                <p className="label">{t('lan_ca_ios_order')}</p>
                <ol className="steps">
                  <li>{t('lan_ca_ios_step_cert')}</li>
                  <li>{t('lan_ca_ios_step_home')}</li>
                  <li>{t('lan_ca_ios_step_pair')}</li>
                </ol>
              </div>
            )}
            <div className="tile-actions">
              <a className="btn" href={status.caUrl} download="nox-home-ca.crt">
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
            {status.caFingerprint && (
              <div className="ca-fingerprint">
                <p className="label">{t('lan_ca_fingerprint')}</p>
                <p className="fingerprint">{shortFingerprint(status.caFingerprint)}</p>
                <p className="hint">{t('lan_ca_fingerprint_hint')}</p>
              </div>
            )}
          </Tile>
        </section>
      )}
    </>
  );
}
