/**
 * Dashboard shell, built like a product site rather than an admin panel: a 48 px translucent top
 * bar carries the wordmark, the eight section links (one WAI-ARIA tablist, centred and tiny) and,
 * on the right, the connection indicator and the appearance control. Everything below is one
 * 1100 px content column: a display headline per section, then tiles and rails.
 *
 * Keyboard-first: skip link, roving-tabindex tablist (arrow keys, Home/End), Alt+1..8 jump to a
 * section from anywhere (and are skipped while a text field has focus), every control reachable and
 * labelled. A global toast panel shows `proactive.notification` events regardless of the section.
 *
 * Tab panels stay **mounted**. Unmounting them destroyed a chat transcript, an unsaved config
 * draft, a clip's tag edit and a Twitch device code on every stray click; `hidden` on the panel is
 * enough to take a section out of the accessibility tree and the tab order. The chat transcript and
 * its draft live here rather than in the page, so they also survive a remount.
 *
 * Below 720 px the link row becomes a horizontal scroll strip; it stays the same single tablist in
 * the DOM, so assistive technology never sees duplicates.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { type Key, type Lang, type T, translator } from './i18n';
import { type ConnStatus, type Envelope, type IpcClient, api, createDashboardClient } from './ipc';
import {
  type ChatMessage,
  type DashboardState,
  INITIAL_STATE,
  applyStateSnapshot,
  dismissNotification,
  parseHealthReport,
  parseProviders,
  reduceEvent,
  setConnected,
} from './model';
import { NotificationToasts } from './NotificationToasts';
import { AuditPage } from './pages/Audit';
import { ChatPage } from './pages/Chat';
import { ClipsPage } from './pages/Clips';
import { HomePage } from './pages/Home';
import { RemotePage } from './pages/Remote';
import { SettingsPage } from './pages/settings/SettingsPage';
import { StatusPage } from './pages/Status';
import { StreamPage } from './pages/Stream';
import { type ThemePref, applyTheme, storeTheme } from './theme';

export type TabId =
  | 'status'
  | 'chat'
  | 'home'
  | 'stream'
  | 'clips'
  | 'remote'
  | 'audit'
  | 'settings';
const TABS: { id: TabId; label: Key }[] = [
  { id: 'status', label: 'tab_status' },
  { id: 'chat', label: 'tab_chat' },
  { id: 'home', label: 'tab_home' },
  { id: 'stream', label: 'tab_stream' },
  { id: 'clips', label: 'tab_clips' },
  { id: 'remote', label: 'tab_remote' },
  { id: 'audit', label: 'tab_audit' },
  { id: 'settings', label: 'tab_settings' },
];

const THEME_OPTIONS: { value: ThemePref; label: Key }[] = [
  { value: 'system', label: 'theme_system' },
  { value: 'light', label: 'theme_light' },
  { value: 'dark', label: 'theme_dark' },
];

/**
 * `ai.providers` probes every provider on a cold cache, which can outlast one request timeout on a
 * machine where the Claude Code CLI still has to be spawned. The first attempt warms the core's
 * cache, so a retry usually succeeds — and when it does not, the Status page says the list could
 * not be loaded instead of claiming the core sent none.
 */
const PROVIDER_RETRIES = 2;
const PROVIDER_RETRY_MS = 4000;

export interface AppProps {
  token: string | null;
  lang: Lang;
  /** Appearance preference read from localStorage before the first paint (see `theme.ts`). */
  theme: ThemePref;
}

/** Typing in a field must not be swallowed by a global accelerator. */
function isTextEntry(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable;
}

export function App({ token, lang, theme: initialTheme }: AppProps) {
  const t: T = useMemo(() => translator(lang), [lang]);
  const [tab, setTab] = useState<TabId>('status');
  const [state, setState] = useState<DashboardState>(INITIAL_STATE);
  const [status, setStatus] = useState<ConnStatus>(token ? 'connecting' : 'offline');
  const [statusDetail, setStatusDetail] = useState<string>('');
  const [client, setClient] = useState<IpcClient | null>(null);
  const [theme, setTheme] = useState<ThemePref>(initialTheme);
  const [providersFailed, setProvidersFailed] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chatDraft, setChatDraft] = useState('');
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);

  const pickTheme = (next: ThemePref) => {
    setTheme(next);
    applyTheme(next);
    storeTheme(next);
  };

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    let live: IpcClient | null = null;
    createDashboardClient(token, {
      onEvent: (env: Envelope) => setState((s) => reduceEvent(s, env.name, env.payload, env.ts)),
      onStatus: (s, detail) => {
        setStatus(s);
        setStatusDetail(detail ?? '');
        setState((prev) => setConnected(prev, s === 'online'));
      },
    })
      .then((c) => {
        live = c;
        if (cancelled) c.close();
        else setClient(c);
      })
      .catch((e: unknown) => {
        // §3: availability is never faked. A client that could not even be built means offline,
        // and the banner says so rather than leaving the page on "verbinde …" forever.
        if (cancelled) return;
        setStatus('offline');
        setStatusDetail(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
      live?.close();
      setClient(null);
    };
  }, [token]);

  // Initial snapshot after every (re)connect: the core is the only source of truth.
  const refresh = useCallback(async (c: IpcClient) => {
    const [snapshot, health] = await Promise.allSettled([api.stateGet(c), api.healthGet(c)]);
    setState((s) => {
      let next = s;
      if (snapshot.status === 'fulfilled') next = applyStateSnapshot(next, snapshot.value);
      if (health.status === 'fulfilled') {
        const parsed = parseHealthReport(health.value);
        if (parsed) next = { ...next, health: parsed };
      }
      return next;
    });
  }, []);

  const loadProviders = useCallback(async (c: IpcClient): Promise<boolean> => {
    try {
      const parsed = parseProviders(await api.providers(c));
      if (!parsed) return false;
      setState((s) => ({ ...s, providers: parsed }));
      setProvidersFailed(false);
      return true;
    } catch {
      return false;
    }
  }, []);

  useEffect(() => {
    if (!client || status !== 'online') return;
    let cancelled = false;
    let timer = 0;
    let attempt = 0;
    void refresh(client);
    const tryProviders = () => {
      void loadProviders(client).then((ok) => {
        if (cancelled || ok) return;
        if (attempt < PROVIDER_RETRIES) {
          attempt += 1;
          timer = window.setTimeout(tryProviders, PROVIDER_RETRY_MS);
        } else {
          setProvidersFailed(true);
        }
      });
    };
    tryProviders();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [client, status, refresh, loadProviders]);

  /** The Status page's "Aktualisieren" link: one fresh snapshot plus one more go at the providers. */
  const refreshAll = useCallback(() => {
    if (!client) return;
    setProvidersFailed(false);
    void refresh(client);
    void loadProviders(client).then((ok) => {
      if (!ok) setProvidersFailed(true);
    });
  }, [client, refresh, loadProviders]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!e.altKey || e.ctrlKey || e.metaKey) return;
      if (isTextEntry(e.target)) return;
      const idx = ['1', '2', '3', '4', '5', '6', '7', '8'].indexOf(e.key);
      const target = TABS[idx];
      if (idx < 0 || !target) return;
      e.preventDefault();
      setTab(target.id);
      tabRefs.current[idx]?.focus();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const onTabKey = (e: React.KeyboardEvent, index: number) => {
    const last = TABS.length - 1;
    const previous = index === 0 ? last : index - 1;
    const following = index === last ? 0 : index + 1;
    // Both axes are handled: the strip is horizontal, but Up/Down cost nothing and are what a
    // list-shaped mental model reaches for.
    const map: Record<string, number> = {
      ArrowRight: following,
      ArrowDown: following,
      ArrowLeft: previous,
      ArrowUp: previous,
      Home: 0,
      End: last,
    };
    const next = map[e.key];
    const target = next === undefined ? undefined : TABS[next];
    if (next === undefined || !target) return;
    e.preventDefault();
    setTab(target.id);
    tabRefs.current[next]?.focus();
  };

  const online = status === 'online';
  const liveClient = online ? client : null;
  const connLabel: Key =
    status === 'online'
      ? 'conn_online'
      : status === 'connecting'
        ? 'conn_connecting'
        : status === 'auth_failed'
          ? 'conn_auth_failed'
          : 'conn_offline';
  const connClass = online
    ? 'conn--online'
    : status === 'auth_failed'
      ? 'conn--error'
      : 'conn--warn';

  return (
    <div className="app">
      <a href="#main" className="skip-link">
        {t('skip_to_main')}
      </a>

      <header className="nav">
        <div className="wrap nav-inner">
          <h1 className="wordmark">{t('app_title')}</h1>

          <nav className="nav-links" aria-label={t('nav')}>
            <div
              role="tablist"
              aria-label={t('nav')}
              aria-orientation="horizontal"
              aria-describedby="nav-shortcuts"
              className="tablist"
            >
              {TABS.map((item, i) => {
                const selected = tab === item.id;
                return (
                  <button
                    key={item.id}
                    ref={(el) => {
                      tabRefs.current[i] = el;
                    }}
                    role="tab"
                    id={`tab-${item.id}`}
                    aria-selected={selected}
                    aria-controls={`panel-${item.id}`}
                    aria-keyshortcuts={`Alt+${i + 1}`}
                    tabIndex={selected ? 0 : -1}
                    onClick={() => setTab(item.id)}
                    onKeyDown={(e) => onTabKey(e, i)}
                    className="tab"
                  >
                    {t(item.label)}
                  </button>
                );
              })}
            </div>
            <p id="nav-shortcuts" className="sr-only">
              {t('shortcuts_hint')}
            </p>
          </nav>

          <div className="nav-aside">
            <p role="status" aria-live="polite" className={`conn ${connClass}`}>
              <span aria-hidden="true" className="dot" />
              <span>
                {t(connLabel)}
                {statusDetail && status === 'auth_failed' ? `: ${statusDetail}` : ''}
              </span>
            </p>
            <fieldset className="seg">
              <legend className="sr-only">{t('theme_label')}</legend>
              {THEME_OPTIONS.map((option) => (
                <label key={option.value} className="seg-item">
                  <input
                    type="radio"
                    name="nox-theme"
                    value={option.value}
                    checked={theme === option.value}
                    onChange={() => pickTheme(option.value)}
                  />
                  <span>{t(option.label)}</span>
                </label>
              ))}
            </fieldset>
          </div>
        </div>
      </header>

      {!token && (
        <p role="alert" className="banner banner--error">
          {t('no_token')}
        </p>
      )}
      {token && !online && status !== 'connecting' && (
        <p role="alert" className="banner">
          {t('offline_hint')}
        </p>
      )}

      <main id="main" className="main" tabIndex={-1}>
        {TABS.map((item) => (
          <div
            key={item.id}
            role="tabpanel"
            id={`panel-${item.id}`}
            aria-labelledby={`tab-${item.id}`}
            hidden={tab !== item.id}
          >
            {item.id === 'status' && (
              <StatusPage
                t={t}
                lang={lang}
                state={state}
                client={liveClient}
                providersFailed={providersFailed}
                onRefresh={refreshAll}
              />
            )}
            {item.id === 'chat' && (
              <ChatPage
                t={t}
                lang={lang}
                client={liveClient}
                providers={state.providers}
                messages={messages}
                onMessages={(update) => setMessages((list) => update(list))}
                draft={chatDraft}
                onDraft={setChatDraft}
              />
            )}
            {item.id === 'home' && (
              <HomePage
                t={t}
                lang={lang}
                state={state}
                client={liveClient}
                onState={setState}
                onOpenSettings={() => setTab('settings')}
              />
            )}
            {item.id === 'stream' && (
              <StreamPage
                t={t}
                lang={lang}
                state={state}
                client={liveClient}
                onState={setState}
                onOpenSettings={() => setTab('settings')}
              />
            )}
            {item.id === 'clips' && (
              <ClipsPage t={t} lang={lang} state={state} client={liveClient} onState={setState} />
            )}
            {item.id === 'remote' && (
              <RemotePage
                t={t}
                lang={lang}
                client={liveClient}
                onOpenSettings={() => setTab('settings')}
              />
            )}
            {item.id === 'audit' && <AuditPage t={t} lang={lang} rows={state.audit} />}
            {item.id === 'settings' && (
              <SettingsPage
                t={t}
                lang={lang}
                client={liveClient}
                revision={state.settingsRevision}
                twitchAuthEvent={state.twitchAuth}
                plugins={state.stream.session.plugins}
                onOpenRemote={() => setTab('remote')}
              />
            )}
          </div>
        ))}
      </main>

      <NotificationToasts
        t={t}
        notifications={state.notifications}
        onDismiss={(id) => setState((s) => dismissNotification(s, id))}
      />
    </div>
  );
}
