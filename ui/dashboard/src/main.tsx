import React from 'react';
import { createRoot } from 'react-dom/client';

import { takeToken } from '../../shared/token';
import './styles.css';
import { App } from './App';
import { pickLang } from './i18n';
import { applyTheme, readTheme } from './theme';

// Token: read once from the fragment, keep in memory only, remove it from the address bar.
const token = takeToken(window.location, window.history);
const lang = pickLang(window.location.search, navigator.languages ?? [navigator.language]);
document.documentElement.lang = lang;

// Appearance before the first paint, so an explicit Light/Dark choice never flashes the other one.
const theme = readTheme();
applyTheme(theme);

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App token={token} lang={lang} theme={theme} />
  </React.StrictMode>,
);
