/**
 * The dashboard's view model: pure parsing and reduction of IPC payloads (Envelope v1 payloads
 * defined in src/nox/core/events.py, src/nox/core/state.py and src/nox/ipc/protocol.py). No React,
 * no side effects, no module-level mutable state — everything here is unit-tested.
 *
 * One file per domain, all re-exported here so import sites read `from '../model'`:
 *   state.ts          the whole `DashboardState` and what "disconnected" clears
 *   health.ts         health report and history
 *   providers.ts      the AI provider list and its display names
 *   stream.ts         stream session, live chat feed, Funken leaderboard
 *   clips.ts          clip records, the pending rows events announce, export results
 *   day.ts            the user's day: appointments, reminders, calendar feeds
 *   desktop.ts        the computer-use notice behind the global banner
 *   home.ts           Home Assistant areas, entities and their live state
 *   localai.ts        the local-model setup: hardware, recommendation, download, probe
 *   voicemodel.ts     the voice-model download: files, size, license, progress, outcome
 *   remote.ts         paired devices and the one-time pairing code
 *   lan.ts            devices in the home network, pairing links, permission prompts
 *   settings.ts       config, secrets, PIN, Twitch device flow, personality
 *   notifications.ts  the toast rows
 *   chat.ts           chat turns and the kill-switch confirm machine
 *   capabilities.ts   MCP servers, learned skills and watchers (the "Fähigkeiten" page)
 *   reduce.ts         `reduceEvent`, the only writer of `DashboardState`
 *   wire.ts           the compile-time link to `shared/generated/ipc.ts`
 */

export * from './capabilities';
export * from './chat';
export * from './clips';
export * from './day';
export * from './desktop';
export * from './health';
export * from './home';
export * from './lan';
export * from './localai';
export * from './presets';
export * from './views';
export * from './voicemodel';
export * from './notifications';
export * from './providers';
export * from './reduce';
export * from './remote';
export * from './settings';
export * from './state';
export * from './stream';
export * from './wire';
