/**
 * The dashboard shell (Spec UX-Neubau §3): four areas - Heute, Nox, Geräte & Fähigkeiten,
 * Einstellungen - in a side bar (tablet, desktop) or a bottom bar (phone), a header with what Nox is
 * doing, what waits for the person, the privacy level and Stopp, a sticky strip for states that must
 * never scroll away, and "Sag Nox, was er tun soll" at the bottom of every page.
 *
 * This file owns the connection and the state that has to outlive a page: the core's snapshot and
 * events, the open permission questions, the chat transcript and its draft. Areas other than Heute
 * load on their first visit and then stay mounted (hidden), so a draft survives a stray tap.
 *
 * Keyboard: skip link, Ctrl/Cmd+K for the command palette (also from a text field), Alt+1..4 for the
 * areas (not while typing), Ctrl+Alt+Pause for Stopp. After every page change focus goes to `main`.
 *
 * While a permission question is open on a page that does not show it inline (anything but Heute
 * and the conversation), it is a sheet and everything behind it is `inert`.
 */

import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';

import type { Auth } from './auth';
import { DeviceContext } from './device';
import { type Lang, type T, fill, translator, withCopy } from './i18n';
import { type ConnStatus, type Envelope, type IpcClient, SIGNED_OUT_DETAIL, api, createDashboardClient } from './ipc';
import {
  type ChatMessage,
  type DashboardState,
  INITIAL_STATE,
  type PermissionPrompt,
  addPrompt,
  applyStateSnapshot,
  clockSkew,
  decidedNote,
  dismissNotification,
  parseHealthReport,
  parseHomeCommand,
  parseLanStatus,
  parsePendingPrompts,
  parsePermissionRequested,
  parseProviders,
  parseRunResult,
  runOutcomeKey,
  reduceEvent,
  removePrompt,
  setConnected,
} from './model';
import { NotificationToasts } from './NotificationToasts';
import type { PairedNotice } from './pages/Devices';
import { useHome } from './pages/Home';
import { useStreamRefresh } from './pages/streamRefresh';
import { type NoteKind, PermissionNote } from './PermissionNote';
import { firstRunDone } from './areas/firstRunFlag';
import { Today } from './areas/Today';
import { CommandBar } from './shell/CommandBar';
import { Header } from './shell/Header';
import { SideNav, TabBar, areaHome } from './shell/Nav';
import type { PaletteItem, PaletteSources } from './shell/palette';
import { presenceOf, troublesOf, waitingTotal } from './shell/presence';
import { AREAS } from './shell/routes';
import { StatusStrip } from './shell/StatusStrip';
import { useAppData } from './shell/useAppData';
import { useRoute } from './shell/useRoute';
import { type ThemePref, applyTheme, storeTheme } from './theme';

const NoxArea = lazy(() => withCopy(() => import('./areas/NoxArea')));
const DevicesArea = lazy(() => withCopy(() => import('./areas/DevicesArea')));
const SettingsArea = lazy(() => withCopy(() => import('./areas/SettingsArea')));
const FirstRun = lazy(() => withCopy(() => import('./areas/FirstRun')));
/** The question itself loads when one is asked; the note above stays in the page. */
const PermissionInline = lazy(() => withCopy(() => import('./PermissionPrompt')).then((m) => ({ default: m.PermissionInline })));
const PermissionPromptDialog = lazy(() =>
  withCopy(() => import('./PermissionPrompt')).then((m) => ({ default: m.PermissionPromptDialog })),
);
/** The palette loads on its first Ctrl+K, not with the page. */
const CommandPalette = lazy(() => withCopy(() => import('./shell/CommandPalette')).then((m) => ({ default: m.CommandPalette })));

/**
 * `ai.providers` probes every provider on a cold cache, which can outlast one request timeout on a
 * machine where the Claude Code CLI still has to be spawned. The first attempt warms the core's
 * cache, so a retry usually succeeds — and when it does not, Einstellungen › KI says the list could
 * not be loaded instead of claiming the core sent none.
 */
const PROVIDER_RETRIES = 2;
const PROVIDER_RETRY_MS = 4000;

/**
 * Why the core refused a paired device (`nox.ipc.tokens`). Only these two mean the device is gone
 * for good and its credentials can be forgotten; anything else keeps them.
 */
const DEVICE_GONE_DETAILS: ReadonlySet<string> = new Set([
  'unknown or revoked device',
  'the token belongs to a different device',
]);
/** Network access was switched off on the machine Nox runs on: the pairing still stands. */
const LAN_OFF_DETAIL = 'network access is off';
/** How often a device that was not let in tries again on its own. */
const LAN_OFF_RETRY_MS = 20000;
/** How long "Erlaubt." / "decided on another device" / "no answer" stays up. */
const NOTE_MS = 6000;

export interface AppProps {
  /** null: opened without a session token and not a paired device (the page says so). */
  auth: Auth | null;
  lang: Lang;
  /** Appearance preference read from localStorage before the first paint (see `theme.ts`). */
  theme: ThemePref;
  /**
   * A paired device was removed (its token is refused) or signed itself out. The caller forgets
   * its credentials and shows the "not paired" screen.
   */
  onDeviceGone?: (reason: 'removed' | 'signed_out') => void;
  /** One line shown under the bar, e.g. that this browser could not store the pairing. */
  notice?: string;
}

/** Typing in a field must not be swallowed by a global accelerator. */
function isTextEntry(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable;
}

export function App({ auth, lang, theme: initialTheme, onDeviceGone, notice }: AppProps) {
  const t: T = useMemo(() => translator(lang), [lang]);
  const device = auth?.kind === 'device';
  const [route, navigate] = useRoute();
  const [state, setState] = useState<DashboardState>(INITIAL_STATE);
  const [status, setStatus] = useState<ConnStatus>(auth ? 'connecting' : 'offline');
  const [prompts, setPrompts] = useState<PermissionPrompt[]>([]);
  const [note, setNote] = useState<{ kind: NoteKind; seq: number } | null>(null);
  const [lanRevision, setLanRevision] = useState(0);
  const [justPaired, setJustPaired] = useState<PairedNotice | null>(null);
  /** On a paired device: whether a security PIN is set (the prompt asks for it then). */
  const [pinSet, setPinSet] = useState<boolean | null>(null);
  /** Bumped to build a new connection, e.g. after "network access is off". */
  const [attempt, setAttempt] = useState(0);
  /** Prompts this dashboard answered itself: their "decided" event is not "elsewhere". */
  const answeredRef = useRef<Set<string>>(new Set());
  /** Prompts this dashboard has shown, kept in step with `prompts` without waiting for a render. */
  const shownRef = useRef<Set<string>>(new Set());
  /** How far the core's clock is ahead of this one, from the latest envelope. */
  const skewRef = useRef(0);
  /** Whether Nox is stopped right now, for the event handler (a question ended by Stopp says so). */
  const stoppedRef = useRef(false);
  const [statusDetail, setStatusDetail] = useState<string>('');
  const [client, setClient] = useState<IpcClient | null>(null);
  const [theme, setTheme] = useState<ThemePref>(initialTheme);
  const [providersFailed, setProvidersFailed] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chatDraft, setChatDraft] = useState('');
  const [outbox, setOutbox] = useState<{ text: string; seq: number } | null>(null);
  const [palette, setPalette] = useState<{ open: boolean; query: string; seq: number }>({
    open: false,
    query: '',
    seq: 0,
  });
  const [stopBusy, setStopBusy] = useState(false);
  const [stopError, setStopError] = useState('');
  const [firstRunClosed, setFirstRunClosed] = useState(firstRunDone);
  /** Areas opened at least once: they stay mounted. */
  const [visited, setVisited] = useState<ReadonlySet<string>>(() => new Set([route.area]));
  if (!visited.has(route.area)) setVisited(new Set([...visited, route.area]));

  const pickTheme = useCallback((next: ThemePref) => {
    setTheme(next);
    applyTheme(next);
    storeTheme(next);
  }, []);

  /** Events the shared reducer does not own: permission prompts and device pairing. */
  const onSideEvent = useCallback((env: Envelope) => {
    skewRef.current = clockSkew(env.ts, Date.now());
    if (env.name === 'security.permission_requested') {
      const prompt = parsePermissionRequested(env.payload, skewRef.current);
      if (prompt) {
        shownRef.current.add(prompt.id);
        setNote(null);
        setPrompts((q) => addPrompt(q, prompt));
      }
    } else if (env.name === 'security.permission_decided') {
      const id = (env.payload as { request_id?: unknown }).request_id;
      if (typeof id === 'string' && shownRef.current.has(id) && !answeredRef.current.has(id)) {
        const kind: NoteKind = stoppedRef.current ? 'stopped' : decidedNote(env.payload);
        setNote((n) => ({ kind, seq: (n?.seq ?? 0) + 1 }));
      }
      if (typeof id === 'string') shownRef.current.delete(id);
      setPrompts((q) => removePrompt(q, env.payload));
    } else if (env.name === 'security.kill_switch') {
      stoppedRef.current = true;
    } else if (env.name === 'remote.paired' || env.name === 'remote.revoked') {
      const payload = env.payload as { channel?: unknown; name?: unknown };
      if (env.name === 'remote.paired' && payload.channel === 'lan' && typeof payload.name === 'string') {
        const name = payload.name;
        setJustPaired((p) => ({ name, seq: (p?.seq ?? 0) + 1 }));
      }
      setLanRevision((n) => n + 1);
    }
  }, []);

  const onAnswered = useCallback((id: string, decision: 'allow' | 'deny') => {
    answeredRef.current.add(id);
    shownRef.current.delete(id);
    setPrompts((q) => q.filter((p) => p.id !== id));
    setNote((n) => ({ kind: decision === 'allow' ? 'allowed' : 'denied', seq: (n?.seq ?? 0) + 1 }));
  }, []);

  useEffect(() => {
    if (!auth) return;
    let cancelled = false;
    let live: IpcClient | null = null;
    createDashboardClient(auth, {
      onEvent: (env: Envelope) => {
        setState((s) => reduceEvent(s, env.name, env.payload, env.ts));
        onSideEvent(env);
      },
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
        // and the strip says so rather than leaving the page on "verbinde …" forever.
        if (cancelled) return;
        setStatus('offline');
        setStatusDetail(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
      live?.close();
      setClient(null);
    };
  }, [auth, onSideEvent, attempt]);

  // "Erlaubt.", "decided on another device" and "no answer" are passing notes, not states.
  useEffect(() => {
    if (!note) return;
    const timer = window.setTimeout(() => setNote(null), NOTE_MS);
    return () => window.clearTimeout(timer);
  }, [note]);

  // A paired device the core refuses: removed (forget it), signed out (forget it, say so), or
  // network access is off (keep the pairing, try again now and then).
  const lanOff = device && status === 'auth_failed' && statusDetail === LAN_OFF_DETAIL;
  useEffect(() => {
    if (!device || status !== 'auth_failed') return;
    if (statusDetail === SIGNED_OUT_DETAIL) onDeviceGone?.('signed_out');
    else if (DEVICE_GONE_DETAILS.has(statusDetail)) onDeviceGone?.('removed');
  }, [device, status, statusDetail, onDeviceGone]);
  useEffect(() => {
    if (!lanOff) return;
    const timer = window.setInterval(() => setAttempt((n) => n + 1), LAN_OFF_RETRY_MS);
    return () => window.clearInterval(timer);
  }, [lanOff]);

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
    let tries = 0;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- refresh() sets state only after awaiting the core; the rule does not see past the await
    void refresh(client);
    // The questions still open replace whatever this page showed before the connection dropped:
    // answered or expired ones go, ones asked while it was away appear.
    void api
      .permissionPending(client)
      .then((payload) => {
        if (cancelled) return;
        const queue = parsePendingPrompts(payload, skewRef.current) ?? [];
        shownRef.current = new Set(queue.map((p) => p.id));
        setPrompts(queue);
      })
      .catch(() => {
        if (cancelled) return;
        shownRef.current = new Set();
        setPrompts([]);
      });
    const tryProviders = () => {
      void loadProviders(client).then((ok) => {
        if (cancelled || ok) return;
        if (tries < PROVIDER_RETRIES) {
          tries += 1;
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

  // On a paired device, whether allowing at high risk can ask for a PIN: read on connect and
  // again whenever a new question comes up, so a PIN set meanwhile is noticed.
  const headPrompt = prompts[0]?.id;
  useEffect(() => {
    if (!device || !client || status !== 'online') return;
    let cancelled = false;
    void api
      .lanStatus(client)
      .then((payload) => {
        if (!cancelled) setPinSet(parseLanStatus(payload)?.pinSet ?? null);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [device, client, status, headPrompt]);

  /** Einstellungen › KI's retry: one fresh snapshot plus one more go at the providers. */
  const refreshAll = useCallback(() => {
    if (!client) return;
    setProvidersFailed(false);
    void refresh(client);
    void loadProviders(client).then((ok) => {
      if (!ok) setProvidersFailed(true);
    });
  }, [client, refresh, loadProviders]);

  const online = status === 'online';
  const liveClient = online ? client : null;
  const stopped = state.systemLevel === 'safe_mode';
  useEffect(() => {
    stoppedRef.current = stopped;
  }, [stopped]);
  const home = useHome(t, liveClient, setState);
  useStreamRefresh(liveClient, setState);
  const data = useAppData(liveClient, device, state.settingsRevision);
  const troubles = troublesOf(state);
  const waiting = waitingTotal({
    prompts: prompts.length,
    facts: data.unconfirmed.length,
    skills: data.skillsReady,
    troubles: troubles.length,
  });

  /** Stopp: at once, no question asked (owner decision); every device may send it. */
  const stop = useCallback(() => {
    if (!client || stopBusy) return;
    setStopBusy(true);
    setStopError('');
    api
      .kill(client, 'dashboard stop')
      .then(() => {
        // The core answered: it is stopped. Its event follows; the strip need not wait for it.
        stoppedRef.current = true;
        setState((s) => ({ ...s, systemLevel: 'safe_mode', stoppedAt: s.stoppedAt ?? new Date().toISOString() }));
      })
      .catch((e: unknown) => setStopError(`${t('ux_stop_failed')} ${e instanceof Error ? e.message : ''}`.trim()))
      .finally(() => setStopBusy(false));
  }, [client, stopBusy, t]);

  const openPalette = useCallback((query: string) => {
    setPalette((p) => ({ open: true, query, seq: p.seq + 1 }));
  }, []);
  const closePalette = useCallback(() => setPalette((p) => ({ ...p, open: false })), []);

  const ask = useCallback(
    (text: string) => {
      setOutbox((o) => ({ text, seq: (o?.seq ?? 0) + 1 }));
      navigate({ area: 'nox', tab: 'talk' });
    },
    [navigate],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        openPalette('');
        return;
      }
      if (e.ctrlKey && e.altKey && (e.key === 'Pause' || e.code === 'Pause')) {
        e.preventDefault();
        stop();
        return;
      }
      if (!e.altKey || e.ctrlKey || e.metaKey || isTextEntry(e.target)) return;
      const idx = ['1', '2', '3', '4'].indexOf(e.key);
      const area = AREAS[idx];
      if (idx < 0 || !area) return;
      e.preventDefault();
      navigate(areaHome(area));
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [navigate, openPalette, stop]);

  const sources: Omit<PaletteSources, 'pages'> = useMemo(
    () => ({
      entities: state.home.entities,
      homeReady: state.home.connected === true,
      presets: data.presets,
      canRunPresets: !device,
      words: {
        turnOn: (name) => fill(t('ux_pal_turn_on'), name),
        turnOff: (name) => fill(t('ux_pal_turn_off'), name),
        scene: (name) => fill(t('ux_pal_scene'), name),
        command: (text) => fill(t('ux_pal_command'), text),
        ask: (text) => fill(t('ux_pal_ask_item'), text),
      },
    }),
    [state.home.entities, state.home.connected, data.presets, t, device],
  );

  const runItem = useCallback(
    async (item: PaletteItem): Promise<string> => {
      const action = item.action;
      if (action.kind === 'goto') {
        closePalette();
        navigate(action.route);
        return '';
      }
      if (action.kind === 'ask') {
        closePalette();
        ask(action.text);
        return '';
      }
      if (!liveClient) return t('perm_offline');
      try {
        switch (action.kind) {
          case 'home': {
            const reply = (await (action.domain === 'light'
              ? api.homeLight(liveClient, [action.entityId], action.on)
              : api.homeSwitch(liveClient, [action.entityId], action.on))) as { ok?: unknown; reason?: unknown } | null;
            if (reply && reply.ok === false) return `${t('home_failed')} ${String(reply.reason ?? '')}`.trim();
            return fill(t('ux_pal_done'), item.label);
          }
          case 'scene':
            await api.homeScene(liveClient, action.entityId);
            return fill(t('ux_pal_done'), item.label);
          case 'command': {
            const result = parseHomeCommand(await api.homeCommand(liveClient, action.text));
            if (!result) return t('home_command_unmatched');
            if (result.refused) return `${t('home_command_refused')} ${result.reason}`.trim();
            if (!result.matched) return t('home_command_unmatched');
            home.refresh();
            return `${t('home_command_done')}: ${result.summary} — ${fill(
              t('home_command_latency'),
              result.matchMs.toLocaleString(lang, { maximumFractionDigits: 1 }),
            )}`;
          }
          case 'preset': {
            const result = parseRunResult(await api.presetsActivate(liveClient, action.id));
            return result ? `${item.label}: ${t(runOutcomeKey(result))}` : fill(t('ux_pal_done'), item.label);
          }
        }
      } catch (e) {
        return `${t('error_prefix')} ${e instanceof Error ? e.message : String(e)}`;
      }
      return '';
    },
    [ask, closePalette, home, lang, liveClient, navigate, t],
  );

  const promptProps = { pinSet, onAnswered };
  const onTalk = route.area === 'nox' && route.tab === 'talk';
  const inlineHere = route.area === 'today' || onTalk;
  const sheetPrompt = prompts.length > 0 && !inlineHere;
  const presence = presenceOf(state, online);
  const offline = Boolean(auth) && !online && status !== 'connecting' && !lanOff;
  // Once started, the first run stays until it is finished or skipped - its own first step sets the
  // name it is triggered by.
  const [firstRunStarted, setFirstRunStarted] = useState(false);
  if (!firstRunStarted && !device && online && data.configLoaded && !data.userName && !firstRunClosed) {
    setFirstRunStarted(true);
  }
  const showFirstRun = firstRunStarted && !firstRunClosed;

  const fallback = (
    <div className="page" aria-busy="true">
      <div className="skeleton skeleton-row" />
      <div className="skeleton skeleton-row" />
    </div>
  );

  return (
    <>
      <div
        className="app"
        inert={sheetPrompt || showFirstRun}
        data-conn={lanOff ? 'lan_off' : status}
        data-area={route.area}
      >
        <a href="#main" className="skip-link">
          {t('skip_to_main')}
        </a>

        <SideNav t={t} route={route} navigate={navigate} />

        <Header
          t={t}
          state={state}
          presence={presence}
          online={online}
          client={liveClient}
          device={device}
          waiting={waiting}
          stopped={stopped}
          onStop={stop}
          stopBusy={stopBusy}
          onWaiting={() => {
            navigate({ area: 'today' });
            window.setTimeout(() => document.getElementById('waiting')?.scrollIntoView?.({ block: 'start' }), 50);
          }}
          onOpenPrivacySettings={() => navigate({ area: 'settings', page: 'privacy' })}
        />

        <StatusStrip
          t={t}
          lang={lang}
          client={liveClient}
          device={device}
          stopped={stopped}
          stoppedAt={state.stoppedAt}
          computerUse={state.computerUse}
          offline={offline}
          lanOff={lanOff}
          onReconnect={() => setAttempt((n) => n + 1)}
          onResumed={() => {
            stoppedRef.current = false;
            setState((s) => ({ ...s, systemLevel: 'running', stoppedAt: null }));
            if (client) void refresh(client);
          }}
        />

        {(!auth || notice || stopError) && (
          <div className="notices">
            {!auth && (
              <p role="alert" className="note note--danger">
                {t('no_token')}
              </p>
            )}
            {notice && (
              <p role="status" className="note">
                {notice}
              </p>
            )}
            {stopError && (
              <p role="alert" className="note note--danger">
                {stopError}
              </p>
            )}
          </div>
        )}

        <DeviceContext.Provider value={device}>
          <main id="main" className="main" tabIndex={-1}>
            <div hidden={route.area !== 'today'}>
              <Today
                t={t}
                lang={lang}
                state={state}
                onState={setState}
                client={liveClient}
                device={device}
                prompts={route.area === 'today' ? prompts : []}
                promptProps={promptProps}
                home={home}
                data={data}
                navigate={navigate}
                onAsk={ask}
              />
            </div>
            <Suspense fallback={fallback}>
              {visited.has('nox') && (
                <div hidden={route.area !== 'nox'}>
                  <NoxArea
                    t={t}
                    lang={lang}
                    state={state}
                    onState={setState}
                    client={liveClient}
                    device={device}
                    tab={route.area === 'nox' ? route.tab : 'talk'}
                    navigate={navigate}
                    messages={messages}
                    onMessages={(update) => setMessages((list) => update(list))}
                    draft={chatDraft}
                    onDraft={setChatDraft}
                    outbox={outbox}
                    stopped={stopped}
                    inlinePrompt={
                      onTalk && prompts.length > 0 ? (
                        <PermissionInline
                          t={t}
                          client={liveClient}
                          device={device}
                          queue={prompts}
                          idBase="talk-perm"
                          {...promptProps}
                        />
                      ) : null
                    }
                  />
                </div>
              )}
              {visited.has('devices') && (
                <div hidden={route.area !== 'devices'}>
                  <DevicesArea
                    t={t}
                    lang={lang}
                    state={state}
                    onState={setState}
                    client={liveClient}
                    device={device}
                    tab={route.area === 'devices' ? route.tab : 'devices'}
                    navigate={navigate}
                    home={home}
                    lanRevision={lanRevision}
                    justPaired={justPaired}
                    onSignedOut={() => onDeviceGone?.('signed_out')}
                  />
                </div>
              )}
              {visited.has('settings') && (
                <div hidden={route.area !== 'settings'}>
                  <SettingsArea
                    t={t}
                    lang={lang}
                    state={state}
                    client={liveClient}
                    device={device}
                    page={route.area === 'settings' ? route.page : null}
                    navigate={navigate}
                    theme={theme}
                    onTheme={pickTheme}
                    providersFailed={providersFailed}
                    onRefreshProviders={refreshAll}
                  />
                </div>
              )}
            </Suspense>
          </main>
        </DeviceContext.Provider>

        {!onTalk && <CommandBar t={t} stopped={stopped} onOpen={openPalette} />}
        <TabBar t={t} route={route} navigate={navigate} />

        <NotificationToasts
          t={t}
          notifications={state.notifications}
          onDismiss={(id) => setState((s) => dismissNotification(s, id))}
        />
      </div>

      {palette.seq > 0 && (
        <Suspense fallback={null}>
          <CommandPalette
            key={palette.seq}
            t={t}
            open={palette.open}
            initialQuery={palette.query}
            sources={sources}
            stopped={stopped}
            onClose={closePalette}
            onRun={runItem}
          />
        </Suspense>
      )}

      {sheetPrompt && (
        <Suspense fallback={null}>
          <PermissionPromptDialog
            t={t}
            client={liveClient}
            queue={prompts}
            device={device}
            pinSet={pinSet}
            onAnswered={onAnswered}
          />
        </Suspense>
      )}
      <PermissionNote t={t} note={note?.kind ?? null} />

      {showFirstRun && (
        <Suspense fallback={null}>
          <FirstRun t={t} client={liveClient} onDone={() => setFirstRunClosed(true)} />
        </Suspense>
      )}
    </>
  );
}
