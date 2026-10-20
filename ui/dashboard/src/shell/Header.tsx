/**
 * The bar on top of every page (Spec UX-Neubau §3, §5): on the left what Nox is doing right now and
 * in which Modus (tapping it opens the Modus menu with "Stumm"), on the right what waits for the
 * person, the privacy level, and Stopp.
 *
 * Stopp acts at once - the owner's decision: a kill switch that asks first is a kill switch that is
 * one step too slow. It is the same `security.kill` every device may send; Ctrl+Alt+Pause does the
 * same from anywhere on the page. While Nox is stopped the button is gone (the strip under the bar
 * offers "Fortsetzen" instead), so nobody presses Stopp twice and wonders what happened.
 */

import { Suspense, lazy, useRef, useState } from 'react';

import { type T, fill, modeHeadline, privacyLabel, withCopy } from '../i18n';
import type { DashboardState } from '../model';
import { Icon } from './icons';
import type { Presence } from './presence';
import { Sheet } from './Sheet';

/** The menus' forms load when a menu opens (the start bundle stays small, Spec §12). */
const ModeChoice = lazy(() => withCopy(() => import('./choices')).then((m) => ({ default: m.ModeChoice })));
const MuteSwitch = lazy(() => withCopy(() => import('./choices')).then((m) => ({ default: m.MuteSwitch })));
const PrivacyChoice = lazy(() => withCopy(() => import('./choices')).then((m) => ({ default: m.PrivacyChoice })));

const PRESENCE_KEY = {
  offline: 'ux_presence_offline',
  stopped: 'ux_presence_stopped',
  live: 'ux_presence_live',
  muted: 'ux_presence_muted',
  speaking: 'ux_presence_speaking',
  listening: 'ux_presence_listening',
  ready: 'ux_presence_ready',
} as const;

export function Lamp({ presence }: { presence: Presence }) {
  return <span className="lamp" data-s={presence} aria-hidden="true" />;
}

export interface HeaderProps {
  t: T;
  state: DashboardState;
  presence: Presence;
  online: boolean;
  /** null while offline: Stopp is then disabled (there is nobody to stop). */
  client: import('../ipc').IpcClient | null;
  device: boolean;
  waiting: number;
  stopped: boolean;
  onStop: () => void;
  stopBusy: boolean;
  onWaiting: () => void;
  onOpenPrivacySettings: () => void;
}

export function Header({
  t,
  state,
  presence,
  online,
  client,
  device,
  waiting,
  stopped,
  onStop,
  stopBusy,
  onWaiting,
  onOpenPrivacySettings,
}: HeaderProps) {
  const [menu, setMenu] = useState<'mode' | 'privacy' | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const privacyCloseRef = useRef<HTMLButtonElement | null>(null);
  const mic = state.capture?.microphone === true;
  const recording = state.capture !== null && (state.capture.camera || state.capture.screen);
  const privacy = state.privacyMode ? privacyLabel(t, state.privacyMode) : t('unknown');

  return (
    <header className="top">
      <p role="status" className="sr-only">
        {t(PRESENCE_KEY[presence])}
      </p>
      <button
        type="button"
        className="presence"
        aria-haspopup="dialog"
        onClick={() => setMenu('mode')}
        disabled={!online}
      >
        <Lamp presence={presence} />
        <span className="presence-t">{t(PRESENCE_KEY[presence])}</span>
        {state.mode && <span className="presence-m">{modeHeadline(t, state.mode)}</span>}
        <span className="sr-only">{t('ux_mode_open')}</span>
      </button>

      <div className="top-r">
        {waiting > 0 && (
          <button
            type="button"
            className="chip chip--wait"
            onClick={onWaiting}
            aria-label={fill(waiting === 1 ? t('ux_waiting_one') : t('ux_waiting_many'), waiting)}
          >
            <Icon name="bell" size={18} />
            <span className="num" aria-hidden="true">
              {waiting}
            </span>
            <span className="chip-long" aria-hidden="true">
              {waiting === 1 ? t('ux_waiting_word_one') : t('ux_waiting_word_many')}
            </span>
          </button>
        )}

        <button
          type="button"
          className="chip"
          aria-haspopup="dialog"
          onClick={() => setMenu('privacy')}
          aria-label={`${t('privacy_title')}: ${privacy}${mic ? `, ${t('ux_mic_on')}` : ''}`}
        >
          <Icon name="shield" size={18} />
          <span className="chip-long" aria-hidden="true">
            {privacy}
          </span>
          {(mic || recording) && (
            <span className="chip-rec" aria-hidden="true">
              {mic && <Icon name="mic" size={16} />}
              {state.capture?.camera && <Icon name="camera" size={16} />}
              {state.capture?.screen && <Icon name="screen" size={16} />}
              <span className="chip-long">{t('ux_on')}</span>
            </span>
          )}
        </button>

        {!stopped && (
          <button
            type="button"
            className="stop"
            onClick={onStop}
            disabled={!client || stopBusy}
            aria-keyshortcuts="Control+Alt+Pause"
            aria-describedby="stop-what"
          >
            <Icon name="stop" size={14} />
            {t('ux_stop')}
          </button>
        )}
        <span id="stop-what" className="sr-only">
          {t('ux_stop_desc')}
        </span>
      </div>

      <Sheet
        open={menu === 'mode'}
        onClose={() => setMenu(null)}
        labelledBy="mode-menu-title"
        variant="pop"
        initialFocus={closeRef}
      >
        <div className="dlg-h">
          <h2 id="mode-menu-title">{t('mode_title')}</h2>
          <button ref={closeRef} type="button" className="ib" aria-label={t('ux_close')} onClick={() => setMenu(null)}>
            <Icon name="close" />
          </button>
        </div>
        <p className="lede">{t('ux_mode_lede')}</p>
        <Suspense fallback={<div className="skeleton skeleton-row" />}>
          <ModeChoice t={t} state={state} client={client} device={device} />
          <MuteSwitch t={t} state={state} client={client} />
        </Suspense>
      </Sheet>

      <Sheet
        open={menu === 'privacy'}
        onClose={() => setMenu(null)}
        labelledBy="privacy-menu-title"
        variant="pop"
        initialFocus={privacyCloseRef}
      >
        <div className="dlg-h">
          <h2 id="privacy-menu-title">{t('privacy_title')}</h2>
          <button
            ref={privacyCloseRef}
            type="button"
            className="ib"
            aria-label={t('ux_close')}
            onClick={() => setMenu(null)}
          >
            <Icon name="close" />
          </button>
        </div>
        <Suspense fallback={<div className="skeleton skeleton-row" />}>
          <PrivacyChoice t={t} state={state} client={client} device={device} />
        </Suspense>
        <div className="dlg-foot">
          <button
            type="button"
            className="btn btn--plain"
            onClick={() => {
              setMenu(null);
              onOpenPrivacySettings();
            }}
          >
            {t('ux_all_settings')}
          </button>
        </div>
      </Sheet>
    </header>
  );
}
