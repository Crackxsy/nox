import React from 'react';
import { createRoot } from 'react-dom/client';

import { pickLang } from '../../shared/lang';
import { queryFlag, queryInt, queryString, takeToken } from '../../shared/token';
import { App } from './App';
// Shared design tokens + pet chrome (#25). Imported here so the whole page, including the
// transparent window's chips and notes, follows the same palette as the dashboard.
import './styles.css';

// Token: read once from the fragment, keep in memory only, remove from the address bar.
const token = takeToken(window.location, window.history);
const overlay = queryFlag(window.location.search, 'overlay');
// OP-1: `?variant=<id>` picks the creature concept (non-secret dev/display flag, query is fine).
const variantId = queryString(window.location.search, 'variant');
// Dev-only still-frame override for render_variants.py: one frame, no WebSocket.
const still = queryFlag(window.location.search, 'still');
const stillExpression = queryString(window.location.search, 'expression');
const size = queryInt(window.location.search, 'size') ?? undefined;
// One language per window (#43/#55): the same rule the dashboard uses, so a German desktop shows
// German in both. `<html lang>` follows, so a screen reader pronounces the words correctly.
const lang = pickLang(window.location.search, navigator.languages ?? []);
document.documentElement.lang = lang;

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App
      token={token}
      lang={lang}
      overlay={overlay}
      variantId={variantId}
      still={still}
      stillExpression={stillExpression}
      size={size}
    />
  </React.StrictMode>,
);
