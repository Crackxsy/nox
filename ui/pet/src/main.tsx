import React from 'react';
import { createRoot } from 'react-dom/client';

import { takeToken, queryFlag, queryInt, queryString } from '../../shared/token';
import { App } from './App';

// Token: read once from the fragment, keep in memory only, remove from the address bar.
const token = takeToken(window.location, window.history);
const overlay = queryFlag(window.location.search, 'overlay');
// OP-1: `?variant=<id>` picks the creature concept (non-secret dev/display flag, query is fine).
const variantId = queryString(window.location.search, 'variant');
// Dev-only still-frame override for render_variants.py: one frame, no WebSocket.
const still = queryFlag(window.location.search, 'still');
const stillExpression = queryString(window.location.search, 'expression');
const size = queryInt(window.location.search, 'size') ?? undefined;

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App
      token={token}
      overlay={overlay}
      variantId={variantId}
      still={still}
      stillExpression={stillExpression}
      size={size}
    />
  </React.StrictMode>,
);
