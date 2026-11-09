import React from 'react';
import { createRoot } from 'react-dom/client';

import { pickLang } from '../../shared/lang';
import { queryFlag, queryInt, queryString, takeCredential } from '../../shared/token';
import { App } from './App';
import { deviceFromQuery } from './ipc';
// Shared design tokens + pet chrome (#25). Imported here so the whole page, including the
// transparent window's chips and notes, follows the same palette as the dashboard.
import './styles.css';

// Credential: read once from the fragment, keep in memory only, remove from the address bar. An
// OBS browser source carries a persistent overlay credential (`#overlay=`), shell windows the
// session token (`#token=`).
const credential = takeCredential(window.location, window.history);
const token = credential?.token ?? null;
const ipcRole = credential?.role === 'overlay' ? 'overlay' : 'pet';
const overlay = queryFlag(window.location.search, 'overlay');
// `?variant=<id>` picks the creature concept (non-secret dev/display flag, query is fine).
const variantId = queryString(window.location.search, 'variant');
// Dev-only still-frame override for render_variants.py: one frame, no WebSocket.
const still = queryFlag(window.location.search, 'still');
// Dev-only motion preview for render_rig.py: the same state presets as `?still=1`, but with the
// animation loop left running, which is the only way to review a rig's motion without a core.
const animate = queryFlag(window.location.search, 'animate');
const stillExpression = queryString(window.location.search, 'expression');
const size = queryInt(window.location.search, 'size') ?? undefined;
// Dev-only, with `?animate=1`: `?tour=idle,happy,feed,...` steps through preview states every
// `?every=` ms (state transitions without a core), `?renderer=rig` forces the older rig renderer
// for before/after recordings of a creature that has been converted to the relief renderer.
const tourParam = animate ? queryString(window.location.search, 'tour') : null;
const tour = tourParam ? tourParam.split(',').filter(Boolean) : null;
const tourEveryMs = queryInt(window.location.search, 'every') ?? 3500;
const forceRig = animate && queryString(window.location.search, 'renderer') === 'rig';
// `?device=<id>`: the overlay a paired node serves (C9) - its pet follows that node's speech.
const device = deviceFromQuery(queryString(window.location.search, 'device'));
// One language per window (#43/#55): the same rule the dashboard uses, so a German desktop shows
// German in both. `<html lang>` follows, so a screen reader pronounces the words correctly.
const lang = pickLang(window.location.search, navigator.languages ?? []);
document.documentElement.lang = lang;

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App
      token={token}
      ipcRole={ipcRole}
      lang={lang}
      overlay={overlay}
      variantId={variantId}
      still={still}
      animate={animate}
      stillExpression={stillExpression}
      size={size}
      tour={tour}
      tourEveryMs={tourEveryMs}
      forceRig={forceRig}
      device={device}
    />
  </React.StrictMode>,
);
