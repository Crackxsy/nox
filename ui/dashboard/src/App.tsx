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
 * Below 1100 px the link row moves onto its own line under the wordmark and the controls, and
 * becomes a horizontal scroll strip: the selected tab is scrolled into view, and a soft fade at an
 * edge says there is more to that side. It stays the same single tablist in the DOM, so assistive
 * technology never sees duplicates.
 *
 * While Nox asks for permission, everything but the question is `inert`: the dialog is modal for
 * the keyboard, the pointer and assistive technology alike.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Auth } from "./auth";
import { DeviceContext } from "./device";
import { type Key, type Lang, type T, translator } from "./i18n";
import {
  type ConnStatus,
  type Envelope,
  type IpcClient,
  SIGNED_OUT_DETAIL,
  api,
  createDashboardClient,
} from "./ipc";
import {
  type ChatMessage,
  type DashboardState,
  type DecidedNote,
  INITIAL_STATE,
  type PermissionPrompt,
  addPrompt,
  applyStateSnapshot,
  clockSkew,
  decidedNote,
  dismissNotification,
  parseHealthReport,
  parseLanStatus,
  parsePendingPrompts,
  parsePermissionRequested,
  parseProviders,
  reduceEvent,
  removePrompt,
  setConnected,
} from "./model";
import { ComputerUseBanner } from "./ComputerUseBanner";
import { NotificationToasts } from "./NotificationToasts";
import { AuditPage } from "./pages/Audit";
import { DevicesSection, type PairedNotice } from "./pages/Devices";
import { PermissionNote, PermissionPromptDialog } from "./PermissionPrompt";
import { BoardPage } from "./pages/Board";
import { CapabilitiesPage } from "./pages/Capabilities";
import { ChatPage } from "./pages/Chat";
import { ClipsPage } from "./pages/Clips";
import { HomePage } from "./pages/Home";
import { PresetsPage } from "./pages/Presets";
import { RemotePage } from "./pages/Remote";
import { SettingsPage } from "./pages/settings/SettingsPage";
import { StatusPage } from "./pages/Status";
import { StreamPage } from "./pages/Stream";
import { type ThemePref, applyTheme, storeTheme } from "./theme";
import { Hero } from "./ui";

export type TabId =
  | "status"
  | "chat"
  | "board"
  | "home"
  | "presets"
  | "stream"
  | "clips"
  | "remote"
  | "audit"
  | "capabilities"
  | "settings";
const TABS: { id: TabId; label: Key }[] = [
  { id: "status", label: "tab_status" },
  { id: "chat", label: "tab_chat" },
  { id: "home", label: "tab_home" },
  { id: "board", label: "tab_board" },
  { id: "presets", label: "tab_presets" },
  { id: "stream", label: "tab_stream" },
  { id: "clips", label: "tab_clips" },
  { id: "remote", label: "tab_remote" },
  { id: "audit", label: "tab_audit" },
  // After Audit, so Alt+1..9 still reach the sections they always did.
  { id: "capabilities", label: "tab_capabilities" },
  { id: "settings", label: "tab_settings" },
];

const THEME_OPTIONS: { value: ThemePref; label: Key }[] = [
  { value: "system", label: "theme_system" },
  { value: "light", label: "theme_light" },
  { value: "dark", label: "theme_dark" },
];

/**
 * `ai.providers` probes every provider on a cold cache, which can outlast one request timeout on a
 * machine where the Claude Code CLI still has to be spawned. The first attempt warms the core's
 * cache, so a retry usually succeeds — and when it does not, the Status page says the list could
 * not be loaded instead of claiming the core sent none.
 */
const PROVIDER_RETRIES = 2;
const PROVIDER_RETRY_MS = 4000;

/**
 * Why the core refused a paired device (`nox.ipc.tokens`). Only these two mean the device is gone
 * for good and its credentials can be forgotten; anything else keeps them.
 */
const DEVICE_GONE_DETAILS: ReadonlySet<string> = new Set([
  "unknown or revoked device",
  "the token belongs to a different device",
]);
/** Network access was switched off on the machine Nox runs on: the pairing still stands. */
const LAN_OFF_DETAIL = "network access is off";
/** How often a device that was not let in tries again on its own. */
const LAN_OFF_RETRY_MS = 20000;
/** How long "decided on another device" / "no answer" stays up. */
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
  onDeviceGone?: (reason: "removed" | "signed_out") => void;
  /** One line shown under the bar, e.g. that this browser could not store the pairing. */
  notice?: string;
}

/**
 * A page a paired device may look at but not operate: one line saying where it works, and every
 * control inside disabled by the browser itself (a disabled fieldset), so nothing is offered that
 * the core would refuse.
 */
function LocalOnly({
  t,
  device,
  note = "device_local_only",
  children,
}: {
  t: T;
  device: boolean;
  note?: Key;
  children: React.ReactNode;
}) {
  if (!device) return <>{children}</>;
  return (
    <>
      <p role="note" className="banner device-note">
        {t(note)}
      </p>
      <fieldset disabled className="fieldset-plain">
        {children}
      </fieldset>
    </>
  );
}

/** Typing in a field must not be swallowed by a global accelerator. */
function isTextEntry(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    target.isContentEditable
  );
}

export function App({
  auth,
  lang,
  theme: initialTheme,
  onDeviceGone,
  notice,
}: AppProps) {
  const t: T = useMemo(() => translator(lang), [lang]);
  const device = auth?.kind === "device";
  const [tab, setTab] = useState<TabId>("status");
  const [state, setState] = useState<DashboardState>(INITIAL_STATE);
  const [status, setStatus] = useState<ConnStatus>(
    auth ? "connecting" : "offline",
  );
  const [prompts, setPrompts] = useState<PermissionPrompt[]>([]);
  const [note, setNote] = useState<{ kind: DecidedNote; seq: number } | null>(
    null,
  );
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
  const tablistRef = useRef<HTMLDivElement | null>(null);
  const [fade, setFade] = useState({ start: false, end: false });
  const [statusDetail, setStatusDetail] = useState<string>("");
  const [client, setClient] = useState<IpcClient | null>(null);
  const [theme, setTheme] = useState<ThemePref>(initialTheme);
  const [providersFailed, setProvidersFailed] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chatDraft, setChatDraft] = useState("");
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);

  const pickTheme = (next: ThemePref) => {
    setTheme(next);
    applyTheme(next);
    storeTheme(next);
  };

  /** Events the shared reducer does not own: permission prompts and device pairing. */
  const onSideEvent = useCallback((env: Envelope) => {
    skewRef.current = clockSkew(env.ts, Date.now());
    if (env.name === "security.permission_requested") {
      const prompt = parsePermissionRequested(env.payload, skewRef.current);
      if (prompt) {
        shownRef.current.add(prompt.id);
        setNote(null);
        setPrompts((q) => addPrompt(q, prompt));
      }
    } else if (env.name === "security.permission_decided") {
      const id = (env.payload as { request_id?: unknown }).request_id;
      if (
        typeof id === "string" &&
        shownRef.current.has(id) &&
        !answeredRef.current.has(id)
      ) {
        const kind = decidedNote(env.payload);
        setNote((n) => ({ kind, seq: (n?.seq ?? 0) + 1 }));
      }
      if (typeof id === "string") shownRef.current.delete(id);
      setPrompts((q) => removePrompt(q, env.payload));
    } else if (env.name === "remote.paired" || env.name === "remote.revoked") {
      const payload = env.payload as { channel?: unknown; name?: unknown };
      if (
        env.name === "remote.paired" &&
        payload.channel === "lan" &&
        typeof payload.name === "string"
      ) {
        const name = payload.name;
        setJustPaired((p) => ({ name, seq: (p?.seq ?? 0) + 1 }));
      }
      setLanRevision((n) => n + 1);
    }
  }, []);

  const onAnswered = useCallback((id: string) => {
    answeredRef.current.add(id);
    shownRef.current.delete(id);
    setPrompts((q) => q.filter((p) => p.id !== id));
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
        setStatusDetail(detail ?? "");
        setState((prev) => setConnected(prev, s === "online"));
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
        setStatus("offline");
        setStatusDetail(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
      live?.close();
      setClient(null);
    };
  }, [auth, onSideEvent, attempt]);

  // "Decided on another device" and "no answer" are passing notes, not states.
  useEffect(() => {
    if (!note) return;
    const timer = window.setTimeout(() => setNote(null), NOTE_MS);
    return () => window.clearTimeout(timer);
  }, [note]);

  // A paired device the core refuses: removed (forget it), signed out (forget it, say so), or
  // network access is off (keep the pairing, try again now and then).
  const lanOff =
    device && status === "auth_failed" && statusDetail === LAN_OFF_DETAIL;
  useEffect(() => {
    if (!device || status !== "auth_failed") return;
    if (statusDetail === SIGNED_OUT_DETAIL) onDeviceGone?.("signed_out");
    else if (DEVICE_GONE_DETAILS.has(statusDetail)) onDeviceGone?.("removed");
  }, [device, status, statusDetail, onDeviceGone]);
  useEffect(() => {
    if (!lanOff) return;
    const timer = window.setInterval(
      () => setAttempt((n) => n + 1),
      LAN_OFF_RETRY_MS,
    );
    return () => window.clearInterval(timer);
  }, [lanOff]);

  // Initial snapshot after every (re)connect: the core is the only source of truth.
  const refresh = useCallback(async (c: IpcClient) => {
    const [snapshot, health] = await Promise.allSettled([
      api.stateGet(c),
      api.healthGet(c),
    ]);
    setState((s) => {
      let next = s;
      if (snapshot.status === "fulfilled")
        next = applyStateSnapshot(next, snapshot.value);
      if (health.status === "fulfilled") {
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
    if (!client || status !== "online") return;
    let cancelled = false;
    let timer = 0;
    let attempt = 0;
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

  // On a paired device, whether allowing at high risk can ask for a PIN: read on connect and
  // again whenever a new question comes up, so a PIN set meanwhile is noticed.
  const headPrompt = prompts[0]?.id;
  useEffect(() => {
    if (!device || !client || status !== "online") return;
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
      const idx = ["1", "2", "3", "4", "5", "6", "7", "8", "9"].indexOf(e.key);
      const target = TABS[idx];
      if (idx < 0 || !target) return;
      e.preventDefault();
      setTab(target.id);
      tabRefs.current[idx]?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // The selected tab is always in view in the scroll strip, also after Alt+number.
  useEffect(() => {
    const index = TABS.findIndex((item) => item.id === tab);
    const reduce =
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    tabRefs.current[index]?.scrollIntoView?.({
      block: "nearest",
      inline: "nearest",
      behavior: reduce ? "auto" : "smooth",
    });
  }, [tab]);

  // A fade at an edge of the strip says there are more tabs that way.
  useEffect(() => {
    const strip = tablistRef.current;
    if (!strip) return;
    const update = () => {
      const max = strip.scrollWidth - strip.clientWidth;
      const next = {
        start: strip.scrollLeft > 2,
        end: strip.scrollLeft < max - 2,
      };
      setFade((f) =>
        f.start === next.start && f.end === next.end ? f : next,
      );
    };
    update();
    strip.addEventListener("scroll", update, { passive: true });
    const observer =
      typeof ResizeObserver === "function" ? new ResizeObserver(update) : null;
    observer?.observe(strip);
    return () => {
      strip.removeEventListener("scroll", update);
      observer?.disconnect();
    };
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

  const online = status === "online";
  const liveClient = online ? client : null;
  const connLabel: Key = lanOff
    ? "conn_lan_off"
    : status === "online"
      ? "conn_online"
      : status === "connecting"
        ? "conn_connecting"
        : status === "auth_failed"
          ? "conn_auth_failed"
          : "conn_offline";
  const asking = prompts.length > 0;
  const connClass = online
    ? "conn--online"
    : status === "auth_failed"
      ? "conn--error"
      : "conn--warn";

  return (
    <>
      <div className="app" inert={asking}>
        <a href="#main" className="skip-link">
          {t("skip_to_main")}
        </a>

        <header className="nav">
          <div className="wrap nav-inner">
            <h1 className="wordmark">{t("app_title")}</h1>

            <nav className="nav-links" aria-label={t("nav")}>
              <div
                ref={tablistRef}
                role="tablist"
                aria-label={t("nav")}
                aria-orientation="horizontal"
                aria-describedby="nav-shortcuts"
                className="tablist"
                data-fade-start={fade.start || undefined}
                data-fade-end={fade.end || undefined}
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
                      // Alt+1..9 exist; a tenth section has no digit and announces no shortcut.
                      aria-keyshortcuts={i < 9 ? `Alt+${i + 1}` : undefined}
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
                {t("shortcuts_hint")}
              </p>
            </nav>

            <div className="nav-aside">
              <p role="status" aria-live="polite" className={`conn ${connClass}`}>
                <span aria-hidden="true" className="dot" />
                <span>
                  {t(connLabel)}
                  {statusDetail && status === "auth_failed" && !device
                    ? `: ${statusDetail}`
                    : ""}
                </span>
              </p>
              <fieldset className="seg">
                <legend className="sr-only">{t("theme_label")}</legend>
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

        <ComputerUseBanner t={t} lang={lang} notice={state.computerUse} />

        {!auth && (
          <p role="alert" className="banner banner--error">
            {t("no_token")}
          </p>
        )}
        {notice && (
          <p role="status" className="banner">
            {notice}
          </p>
        )}
        {lanOff ? (
          <div role="alert" className="banner banner--action">
            <p>{t("device_lan_off")}</p>
            <button
              type="button"
              className="link link--plain"
              onClick={() => setAttempt((n) => n + 1)}
            >
              {t("device_reconnect")}
            </button>
          </div>
        ) : (
          auth &&
          !online &&
          status !== "connecting" && (
            <p role="alert" className="banner">
              {t("offline_hint")}
            </p>
          )
        )}

        <DeviceContext.Provider value={device}>
          <main id="main" className="main" tabIndex={-1}>
            {TABS.map((item) => (
              <div
                key={item.id}
                role="tabpanel"
                id={`panel-${item.id}`}
                aria-labelledby={`tab-${item.id}`}
                hidden={tab !== item.id}
              >
                {item.id === "status" && (
                  <StatusPage
                    t={t}
                    lang={lang}
                    state={state}
                    client={liveClient}
                    providersFailed={providersFailed}
                    onRefresh={refreshAll}
                  />
                )}
                {item.id === "chat" && (
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
                {item.id === "home" && (
                  <HomePage
                    t={t}
                    lang={lang}
                    state={state}
                    client={liveClient}
                    onState={setState}
                    onOpenSettings={() => setTab("settings")}
                  />
                )}
                {item.id === "board" && (
                  <BoardPage
                    t={t}
                    lang={lang}
                    client={liveClient}
                    revision={state.viewsRevision}
                  />
                )}
                {item.id === "presets" && (
                  <LocalOnly t={t} device={device}>
                    <PresetsPage t={t} client={liveClient} />
                  </LocalOnly>
                )}
                {item.id === "stream" && (
                  <StreamPage
                    t={t}
                    lang={lang}
                    state={state}
                    client={liveClient}
                    onState={setState}
                    onOpenSettings={() => setTab("settings")}
                  />
                )}
                {item.id === "clips" && (
                  <LocalOnly t={t} device={device}>
                    <ClipsPage
                      t={t}
                      lang={lang}
                      state={state}
                      client={liveClient}
                      onState={setState}
                    />
                  </LocalOnly>
                )}
                {item.id === "remote" && (
                  <div className="page wrap">
                    <Hero title={t("hero_remote")} sub={t("hero_remote_sub")} />
                    <DevicesSection
                      t={t}
                      lang={lang}
                      client={liveClient}
                      device={device}
                      revision={lanRevision}
                      justPaired={justPaired}
                      onOpenSettings={() => setTab("settings")}
                      onSignedOut={() => onDeviceGone?.("signed_out")}
                    />
                    {device ? (
                      <p className="hint section-note">
                        {t("remote_device_note")}
                      </p>
                    ) : (
                      <RemotePage
                        t={t}
                        lang={lang}
                        client={liveClient}
                        onOpenSettings={() => setTab("settings")}
                        embedded
                      />
                    )}
                  </div>
                )}
                {item.id === "audit" && (
                  <AuditPage t={t} lang={lang} rows={state.audit} />
                )}
                {item.id === "capabilities" && (
                  <LocalOnly t={t} device={device} note="device_admin_note">
                    <CapabilitiesPage t={t} lang={lang} client={liveClient} />
                  </LocalOnly>
                )}
                {item.id === "settings" && (
                  <LocalOnly t={t} device={device} note="device_admin_note">
                    <SettingsPage
                      t={t}
                      lang={lang}
                      client={liveClient}
                      revision={state.settingsRevision}
                      twitchAuthEvent={state.twitchAuth}
                      plugins={state.stream.session.plugins}
                      onOpenRemote={() => setTab("remote")}
                    />
                  </LocalOnly>
                )}
              </div>
            ))}
          </main>
        </DeviceContext.Provider>

        <NotificationToasts
          t={t}
          notifications={state.notifications}
          onDismiss={(id) => setState((s) => dismissNotification(s, id))}
        />
      </div>

      <PermissionPromptDialog
        t={t}
        client={liveClient}
        queue={prompts}
        device={device}
        pinSet={pinSet}
        onAnswered={onAnswered}
      />
      <PermissionNote t={t} note={note?.kind ?? null} />
    </>
  );
}
