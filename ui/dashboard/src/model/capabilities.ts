/**
 * "Fähigkeiten": other people's MCP servers, the skills Nox taught itself, and the watchers behind
 * "sag mir, wenn ...". Wire types for `mcp.servers.*`, `skills.*` and `watch.*`, their parsers, and
 * the pure helpers of the add-server form.
 *
 * Narrowed by hand through `shared/guards` like `localai.ts`: these requests are answered by the
 * `mcp`, `skills` and `watch` extensions, not by pydantic models in `src/nox/ipc/protocol.py`.
 *
 * Two kinds of text arrive here that nobody vouched for: a tool description an MCP server sends
 * about itself, and what a model wrote into a learned skill. They are kept as plain strings; the
 * page renders them as text and labels them, never as markup.
 */

import { bool, isRecord, num, numOrNull, str, strOrNull, stringList } from '../../../shared/guards';

/* ------------------------------------------------------------------ MCP - */

export type McpTransport = 'stdio' | 'http';

export type McpServerState =
  | 'disabled'
  | 'blocked'
  | 'needs_approval'
  | 'starting'
  | 'running'
  | 'failed'
  | 'stopped';

const MCP_STATES: readonly McpServerState[] = [
  'disabled',
  'blocked',
  'needs_approval',
  'starting',
  'running',
  'failed',
  'stopped',
];

export interface McpTool {
  /** Nox's name for it (`mcp.<id>.<tool>`); empty for a refused tool. */
  name: string;
  /** The name on the server. Untrusted text. */
  remoteName: string;
  /** What the server says the tool does. Untrusted text, never markup. */
  description: string;
  /** The user declared it read-only. */
  readOnly: boolean;
  /** The server claims it is read-only; shown, never trusted. */
  serverSaysReadOnly: boolean;
  /** Why Nox refused the tool; empty when it is usable. */
  blocked: string;
}

export interface McpServer {
  id: string;
  name: string;
  transport: McpTransport;
  command: string[];
  url: string;
  version: string;
  enabled: boolean;
  network: boolean;
  /** An unknown state word from a newer core stays visible instead of being mapped to a guess. */
  state: McpServerState | string;
  reason: string;
  sandboxed: boolean;
  /** Names of the secrets the server's environment or headers refer to. */
  secrets: string[];
  tools: McpTool[];
}

export interface McpServerList {
  /** Whether local servers run inside the sandbox at all on this machine. */
  sandboxed: boolean;
  servers: McpServer[];
}

function parseMcpTool(raw: unknown): McpTool | null {
  if (!isRecord(raw)) return null;
  return {
    name: str(raw.name),
    remoteName: str(raw.remote_name),
    description: str(raw.description),
    readOnly: bool(raw.read_only),
    serverSaysReadOnly: bool(raw.server_says_read_only),
    blocked: str(raw.blocked),
  };
}

function parseMcpServer(raw: unknown): McpServer | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  const tools = Array.isArray(raw.tools) ? raw.tools.map(parseMcpTool) : [];
  return {
    id,
    name: str(raw.name) || id,
    transport: raw.transport === 'http' ? 'http' : 'stdio',
    command: stringList(raw.command),
    url: str(raw.url),
    version: str(raw.version),
    enabled: bool(raw.enabled),
    network: bool(raw.network),
    state: str(raw.state, 'stopped'),
    reason: str(raw.reason),
    sandboxed: bool(raw.sandboxed),
    secrets: stringList(raw.secrets),
    tools: tools.filter((tool): tool is McpTool => tool !== null),
  };
}

export function parseMcpServers(payload: unknown): McpServerList {
  if (!isRecord(payload)) return { sandboxed: false, servers: [] };
  const servers = Array.isArray(payload.servers) ? payload.servers.map(parseMcpServer) : [];
  return {
    sandboxed: bool(payload.sandboxed),
    servers: servers.filter((server): server is McpServer => server !== null),
  };
}

export function isKnownMcpState(state: string): state is McpServerState {
  return (MCP_STATES as readonly string[]).includes(state);
}

/** `{ok, reason?, state?, needs_pin?}` — the answer of every write request on this page. */
export interface ActionResult {
  ok: boolean;
  /** The core's reason for a refusal, shown verbatim. */
  reason: string;
  state: string;
  needsPin: boolean;
}

export function parseActionResult(payload: unknown): ActionResult {
  if (!isRecord(payload)) return { ok: false, reason: '', state: '', needsPin: false };
  return {
    ok: bool(payload.ok),
    reason: str(payload.reason),
    state: str(payload.state),
    needsPin: bool(payload.needs_pin),
  };
}

/* ------------------------------------------------- the add-server form - */

/** `SERVER_ID_PATTERN` in `src/nox/core/config/hands.py`. */
export const SERVER_ID_RE = /^[a-z][a-z0-9_]{1,31}$/;

/** The key part a `secrets.set` name allows (`DYNAMIC_SECRETS` in `secrets_ipc.py`). */
const SECRET_KEY_RE = /^[a-z0-9_\-.]{1,64}$/;

/** Programs that fetch a package by name at start, so the user must pin a version. */
const PINNED_RUNNERS = new Set(['npx', 'pnpx', 'bunx', 'uvx']);

export interface KeyValueRow {
  key: string;
  value: string;
  /** Stored in the secret store; only a `secret:` reference reaches `mcp.servers.add`. */
  secret: boolean;
}

export interface McpServerDraft {
  kind: 'program' | 'address';
  id: string;
  name: string;
  commandLine: string;
  url: string;
  version: string;
  network: boolean;
  /** One tool name per line. */
  readOnlyTools: string;
  /** Environment variables (program) or headers (address). */
  rows: KeyValueRow[];
}

export const BLANK_SERVER_DRAFT: McpServerDraft = {
  kind: 'program',
  id: '',
  name: '',
  commandLine: '',
  url: '',
  version: '',
  network: true,
  readOnlyTools: '',
  rows: [],
};

/**
 * A command line -> argv, split on whitespace. Double or single quotes keep a part together and
 * are removed; a backslash is an ordinary character, so a Windows path survives as typed.
 * `null` when a quote is left open.
 */
export function splitCommandLine(line: string): string[] | null {
  const parts: string[] = [];
  let current = '';
  let inPart = false;
  let quote: '"' | "'" | null = null;
  for (const ch of line) {
    if (quote) {
      if (ch === quote) quote = null;
      else current += ch;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      inPart = true;
    } else if (/\s/.test(ch)) {
      if (inPart) parts.push(current);
      current = '';
      inPart = false;
    } else {
      current += ch;
      inPart = true;
    }
  }
  if (quote) return null;
  if (inPart) parts.push(current);
  return parts;
}

/** `C:\\tools\\npx.cmd` -> `npx`: the program a command starts, lower case, without suffix. */
export function programName(argv: string[]): string {
  const first = argv[0] ?? '';
  const base = first.split(/[\\/]/).pop() ?? '';
  return base.toLowerCase().replace(/\.(cmd|exe|bat|ps1)$/, '');
}

/** Whether this command fetches a package at start and therefore needs a pinned version. */
export function needsPinnedVersion(argv: string[]): boolean {
  return PINNED_RUNNERS.has(programName(argv));
}

/** `nox/mcp/<id>/<key, lower case>`: where a secret value of this server is stored. */
export function secretName(serverId: string, key: string): string {
  return `nox/mcp/${serverId}/${key.trim().toLowerCase()}`;
}

/** What `mcp.servers.add` gets instead of the secret itself. */
export function secretRef(serverId: string, key: string): string {
  return `secret:${secretName(serverId, key)}`;
}

/** Why the draft cannot be sent yet, as a dictionary key; `null` when it can. */
export type DraftProblem =
  | 'caps_add_err_id'
  | 'caps_add_err_id_taken'
  | 'caps_add_err_command'
  | 'caps_add_err_quote'
  | 'caps_add_err_url'
  | 'caps_add_err_version'
  | 'caps_add_err_key'
  | 'caps_add_err_key_twice'
  | 'caps_add_err_secret_key'
  | 'caps_add_err_secret_empty';

export function draftProblem(draft: McpServerDraft, takenIds: readonly string[]): DraftProblem | null {
  if (!SERVER_ID_RE.test(draft.id)) return 'caps_add_err_id';
  if (takenIds.includes(draft.id)) return 'caps_add_err_id_taken';
  if (draft.kind === 'program') {
    const argv = splitCommandLine(draft.commandLine);
    if (argv === null) return 'caps_add_err_quote';
    if (argv.length === 0) return 'caps_add_err_command';
    if (needsPinnedVersion(argv) && draft.version.trim() === '') return 'caps_add_err_version';
  } else if (!/^https:\/\/\S+$/i.test(draft.url.trim())) {
    return 'caps_add_err_url';
  }
  const keys = new Set<string>();
  for (const row of draft.rows) {
    const key = row.key.trim();
    if (key === '' && row.value === '') continue;
    if (key === '') return 'caps_add_err_key';
    if (keys.has(key)) return 'caps_add_err_key_twice';
    keys.add(key);
    if (row.secret) {
      if (!SECRET_KEY_RE.test(key.toLowerCase())) return 'caps_add_err_secret_key';
      if (row.value === '') return 'caps_add_err_secret_empty';
    }
  }
  return null;
}

/** The rows that carry something; a blank trailing row is not sent. */
export function filledRows(rows: KeyValueRow[]): KeyValueRow[] {
  return rows.filter((row) => row.key.trim() !== '');
}

export interface McpServerAddPayload {
  id: string;
  name: string;
  command: string[];
  url: string;
  version: string;
  env: Record<string, string>;
  headers: Record<string, string>;
  network: boolean;
  read_only_tools: string[];
}

/**
 * The `mcp.servers.add` payload. A row marked secret becomes its `secret:` reference here; its
 * value is never part of this object, which is the whole point.
 */
export function addPayload(draft: McpServerDraft): McpServerAddPayload {
  const values: Record<string, string> = {};
  for (const row of filledRows(draft.rows)) {
    const key = row.key.trim();
    values[key] = row.secret ? secretRef(draft.id, key) : row.value;
  }
  const program = draft.kind === 'program';
  return {
    id: draft.id,
    name: draft.name.trim(),
    command: program ? (splitCommandLine(draft.commandLine) ?? []) : [],
    url: program ? '' : draft.url.trim(),
    version: draft.version.trim(),
    env: program ? values : {},
    headers: program ? {} : values,
    network: program ? draft.network : true,
    read_only_tools: draft.readOnlyTools
      .split('\n')
      .map((line) => line.trim())
      .filter((line) => line !== ''),
  };
}

/** The secrets to store first: `{name, value}` per row marked secret. */
export function draftSecrets(draft: McpServerDraft): { name: string; value: string }[] {
  return filledRows(draft.rows)
    .filter((row) => row.secret)
    .map((row) => ({ name: secretName(draft.id, row.key), value: row.value }));
}

/* --------------------------------------------------------------- skills - */

export type SkillState = 'building' | 'ready' | 'failed' | 'active' | 'inactive';

export interface SkillTool {
  name: string;
  /** `low` / `medium` / `high` / `critical`, as the skill's manifest declares. */
  risk: string;
  /** Written by the model that built the skill. Untrusted text. */
  description: string;
}

export interface SkillSummary {
  tools: SkillTool[];
  hosts: string[];
  /** Hosts no profile allows yet; activating approves them. */
  newHosts: string[];
  secrets: string[];
  secretsPresent: Record<string, boolean>;
  testsPassed: number;
  testsFailed: number;
  testOutput: string;
  needsPin: boolean;
}

export interface Skill {
  id: string;
  intent: string;
  state: SkillState | string;
  createdAt: string | null;
  updatedAt: string | null;
  version: number;
  rounds: number;
  problems: string[];
  message: string;
  approvedHosts: string[];
  summary: SkillSummary | null;
}

export interface SkillList {
  /** Whether Nox can learn something new right now. */
  available: boolean;
  /** Why not, in the core's words. */
  reason: string;
  skills: Skill[];
}

function parseSkillSummary(raw: unknown): SkillSummary | null {
  if (!isRecord(raw)) return null;
  const tools = Array.isArray(raw.tools)
    ? raw.tools.filter(isRecord).map((tool) => ({
        name: str(tool.name),
        risk: str(tool.risk),
        description: str(tool.description),
      }))
    : [];
  const present: Record<string, boolean> = {};
  if (isRecord(raw.secrets_present)) {
    for (const [name, value] of Object.entries(raw.secrets_present)) present[name] = bool(value);
  }
  return {
    tools,
    hosts: stringList(raw.hosts),
    newHosts: stringList(raw.new_hosts),
    secrets: stringList(raw.secrets),
    secretsPresent: present,
    testsPassed: num(raw.tests_passed, 0),
    testsFailed: num(raw.tests_failed, 0),
    testOutput: str(raw.test_output),
    needsPin: bool(raw.needs_pin),
  };
}

function parseSkill(raw: unknown): Skill | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  return {
    id,
    intent: str(raw.intent),
    state: str(raw.state),
    createdAt: strOrNull(raw.created_at),
    updatedAt: strOrNull(raw.updated_at),
    version: num(raw.version, 0),
    rounds: num(raw.rounds, 0),
    problems: stringList(raw.problems),
    message: str(raw.message),
    approvedHosts: stringList(raw.approved_hosts),
    summary: parseSkillSummary(raw.summary),
  };
}

export function parseSkills(payload: unknown): SkillList {
  if (!isRecord(payload)) return { available: false, reason: '', skills: [] };
  const skills = Array.isArray(payload.skills) ? payload.skills.map(parseSkill) : [];
  return {
    available: bool(payload.available),
    reason: str(payload.reason),
    skills: skills.filter((skill): skill is Skill => skill !== null),
  };
}

export function anyBuilding(list: SkillList | null): boolean {
  return list?.skills.some((skill) => skill.state === 'building') ?? false;
}

/** Secrets the summary names that are not stored yet. */
export function missingSecrets(summary: SkillSummary | null): string[] {
  if (!summary) return [];
  return summary.secrets.filter((name) => summary.secretsPresent[name] !== true);
}

/* -------------------------------------------------------------- watchers - */

export type WatchOp = 'lt' | 'gt' | 'contains' | 'changed';

export interface WatchCondition {
  /** Where in the tool's answer the value is (dotted keys, list indexes); may be empty. */
  path: string;
  /** For text answers: only the rest of the line after this literal text counts. */
  near: string;
  op: WatchOp | string;
  /** Threshold or text; `null` for `changed`. */
  value: string | null;
}

export interface Watcher {
  id: string;
  title: string;
  tool: string;
  args: Record<string, unknown>;
  condition: WatchCondition;
  intervalMin: number | null;
  cooldownMin: number | null;
  createdAt: string | null;
  lastCheckedAt: string | null;
  lastValue: string | null;
  lastFiredAt: string | null;
  lastError: string;
  checks: number;
  fired: number;
}

function scalarText(x: unknown): string | null {
  if (typeof x === 'string') return x;
  if (typeof x === 'number' && Number.isFinite(x)) return String(x);
  if (typeof x === 'boolean') return String(x);
  return null;
}

function parseWatcher(raw: unknown): Watcher | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  const condition = isRecord(raw.condition) ? raw.condition : {};
  return {
    id,
    title: str(raw.title),
    tool: str(raw.tool),
    args: isRecord(raw.args) ? raw.args : {},
    condition: {
      path: str(condition.path),
      near: str(condition.near),
      op: str(condition.op),
      value: scalarText(condition.value),
    },
    intervalMin: numOrNull(raw.interval_min),
    cooldownMin: numOrNull(raw.cooldown_min),
    createdAt: strOrNull(raw.created_at),
    lastCheckedAt: strOrNull(raw.last_checked_at),
    lastValue: scalarText(raw.last_value),
    lastFiredAt: strOrNull(raw.last_fired_at),
    lastError: str(raw.last_error),
    checks: num(raw.checks, 0),
    fired: num(raw.fired, 0),
  };
}

export function parseWatchers(payload: unknown): Watcher[] {
  if (!isRecord(payload) || !Array.isArray(payload.watchers)) return [];
  return payload.watchers.map(parseWatcher).filter((w): w is Watcher => w !== null);
}
