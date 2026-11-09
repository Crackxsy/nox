/**
 * Geräte & Fähigkeiten: what Nox is connected to and what it can do (Spec UX-Neubau §3).
 *
 * "Geräte" is everything Nox is connected to - computers and servers paired as nodes (spec C
 * §1.10, above the phones), phones, tablets and PCs (pairing in the home network, and Telegram),
 * the rooms of the house, the stream technology. "Fähigkeiten" is the
 * Abläufe, the Wächter and, set apart, what is for advanced users. Loaded on first visit.
 *
 * On a paired device the administration stays read-only: the Fähigkeiten tab is a disabled
 * fieldset with one line saying where it works, and Telegram pairing is not offered at all - the
 * core refuses both to a device role.
 */

import { Suspense, lazy } from 'react';

import type { Lang, T } from '../i18n';
import type { IpcClient } from '../ipc';
import type { DashboardState } from '../model';
import { CapabilitiesPage } from '../pages/Capabilities';
import { DevicesSection, type PairedNotice } from '../pages/Devices';
import { type HomeController, HomeRooms } from '../pages/Home';
import { RemotePage } from '../pages/Remote';
import { StreamTech } from '../pages/Stream';
import { ChunkBoundary } from '../shell/ChunkBoundary';
import { LocalOnly } from '../shell/LocalOnly';
import type { DevicesTab, Route } from '../shell/routes';
import { SubTabs } from '../shell/SubTabs';

// Its own chunk: the area stays within its budget (Spec UX-Neubau §12), and the copy is loaded
// with the area already.
const ComputersSection = lazy(() => import('../pages/Nodes').then((m) => ({ default: m.ComputersSection })));

export interface DevicesAreaProps {
  t: T;
  lang: Lang;
  state: DashboardState;
  onState: (updater: (s: DashboardState) => DashboardState) => void;
  client: IpcClient | null;
  device: boolean;
  tab: DevicesTab;
  navigate: (route: Route) => void;
  home: HomeController;
  lanRevision: number;
  justPaired: PairedNotice | null;
  onSignedOut: () => void;
}

export default function DevicesArea(props: DevicesAreaProps) {
  const { t, lang, state, onState, client, device, tab, navigate, home } = props;
  return (
    <div className="page">
      <header className="page-h">
        <h1>{t('ux_area_devices')}</h1>
      </header>
      <SubTabs<DevicesTab>
        label={t('ux_area_devices')}
        idBase="devices"
        active={tab}
        onSelect={(next) => navigate({ area: 'devices', tab: next })}
        tabs={[
          {
            id: 'devices',
            label: t('ux_tab_devices'),
            render: () => (
              <>
                <ChunkBoundary t={t}>
                  <Suspense fallback={null}>
                    <ComputersSection
                      t={t}
                      lang={lang}
                      client={client}
                      device={device}
                      revision={props.lanRevision}
                      audit={state.audit}
                      onOpenSettings={() => navigate({ area: 'settings', page: 'privacy' })}
                      onOpenHistory={() => navigate({ area: 'nox', tab: 'history' })}
                    />
                  </Suspense>
                </ChunkBoundary>
                <section className="sec" aria-labelledby="h-dev-phones">
                  <h2 id="h-dev-phones" className="sec-title">
                    {t('ux_dev_phones')}
                  </h2>
                  <DevicesSection
                    t={t}
                    lang={lang}
                    client={client}
                    device={device}
                    revision={props.lanRevision}
                    justPaired={props.justPaired}
                    onOpenSettings={() => navigate({ area: 'settings', page: 'privacy' })}
                    onSignedOut={props.onSignedOut}
                  />
                  {device ? (
                    <p className="hint section-note">{t('remote_device_note')}</p>
                  ) : (
                    // Telegram is one quiet line while it is off (the page folds itself).
                    <RemotePage
                      t={t}
                      lang={lang}
                      client={client}
                      onOpenSettings={() => navigate({ area: 'settings', page: 'privacy' })}
                      embedded
                    />
                  )}
                </section>
                <section className="sec" aria-labelledby="h-dev-home">
                  <h2 id="h-dev-home" className="sec-title">
                    {t('ux_home_title')}
                  </h2>
                  <HomeRooms
                    t={t}
                    lang={lang}
                    state={state}
                    client={client}
                    home={home}
                    onOpenSettings={() => navigate({ area: 'settings', page: 'home' })}
                  />
                </section>
                <section className="sec" aria-labelledby="h-dev-stream">
                  <h2 id="h-dev-stream" className="sec-title">
                    {t('ux_dev_stream')}
                  </h2>
                  <StreamTech
                    t={t}
                    lang={lang}
                    state={state}
                    client={client}
                    onState={onState}
                    device={device}
                    onOpenStreamSettings={() => navigate({ area: 'settings', page: 'stream' })}
                  />
                </section>
              </>
            ),
          },
          {
            id: 'skills',
            label: t('ux_tab_skills'),
            render: () => (
              <LocalOnly t={t} device={device} note="device_admin_note">
                <CapabilitiesPage t={t} lang={lang} client={client} />
              </LocalOnly>
            ),
          },
        ]}
      />
    </div>
  );
}
