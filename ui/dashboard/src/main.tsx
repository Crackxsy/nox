import React, { useCallback, useState } from 'react';
import { createRoot } from 'react-dom/client';

import { takeToken } from '../../shared/token';
import './styles.css';
import { App } from './App';
import { type Auth, type DeviceCredentials, forgetDevice, loadDevice, takePairCode } from './auth';
import { PairScreen, type UnpairedReason, UnpairedScreen } from './Gate';
import { type Lang, pickLang, translator } from './i18n';
import { currentPlatform, isStandalone } from './platform';
import { type ThemePref, applyTheme, readTheme } from './theme';

// Token: read once from the fragment, keep in memory only, remove it from the address bar.
const token = takeToken(window.location, window.history);
// A pairing link (`#pair=`), taken out of the address bar the same way.
const pairCode = takePairCode(window.location, window.history);
const lang = pickLang(window.location.search, navigator.languages ?? [navigator.language]);
document.documentElement.lang = lang;
/** Served by the network listener: only paired devices get in, over HTTPS. */
const viaNetwork = window.location.protocol === 'https:';

// Appearance before the first paint, so an explicit Light/Dark choice never flashes the other one.
const theme = readTheme();
applyTheme(theme);

// The app shell, so a device can add Nox to its home screen. Only on the network listener's own
// origin; the PC's dashboard is opened by the shell and has no use for it.
if (viaNetwork && 'serviceWorker' in navigator) {
  void navigator.serviceWorker.register('sw.js', { scope: './' }).catch(() => undefined);
}

type Screen =
  | { kind: 'app'; auth: Auth | null; notice?: string }
  | { kind: 'pair'; code: string | null }
  | { kind: 'unpaired'; reason: UnpairedReason };

function initialScreen(): Screen {
  if (token) return { kind: 'app', auth: { kind: 'local', token } };
  if (pairCode) return { kind: 'pair', code: pairCode };
  const device = loadDevice();
  if (device) return { kind: 'app', auth: { kind: 'device', ...device } };
  // On the PC without a token, the dashboard explains how to open it properly (as before).
  if (!viaNetwork) return { kind: 'app', auth: null };
  // Started from the home screen for the first time: on iOS that app has storage of its own, so it
  // is paired on its own - straight to the code entry, which is the only thing to do here.
  if (isStandalone(currentPlatform())) return { kind: 'pair', code: null };
  return { kind: 'unpaired', reason: 'never' };
}

function Root({ language, appearance }: { language: Lang; appearance: ThemePref }) {
  const [screen, setScreen] = useState<Screen>(initialScreen);
  const t = translator(language);

  const paired = useCallback(
    (creds: DeviceCredentials, stored: boolean) =>
      setScreen({
        kind: 'app',
        auth: { kind: 'device', ...creds },
        notice: stored ? undefined : t('pair_no_storage'),
      }),
    [t],
  );

  const gone = useCallback((reason: 'removed' | 'signed_out') => {
    forgetDevice();
    setScreen({ kind: 'unpaired', reason });
  }, []);

  if (screen.kind === 'pair') return <PairScreen t={t} code={screen.code} onPaired={paired} />;
  if (screen.kind === 'unpaired') {
    return (
      <UnpairedScreen
        t={t}
        reason={screen.reason}
        onHaveCode={() => setScreen({ kind: 'pair', code: null })}
      />
    );
  }
  return (
    <App
      auth={screen.auth}
      lang={language}
      theme={appearance}
      onDeviceGone={gone}
      notice={screen.notice}
    />
  );
}

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Root language={lang} appearance={theme} />
  </React.StrictMode>,
);
