/**
 * GENERATED — do not edit by hand.
 *
 * Source of truth: `src/nox/ipc/protocol.py` and `src/nox/core/events.py` (PAYLOAD_MODELS).
 * Regenerate with:
 *   .venv/Scripts/python.exe scripts/gen_ts_types.py
 * `scripts/gen_ts_types.py --check` fails CI when this file is stale (OP-9, Decision Plan
 * 2026-09-11: the pydantic models are authoritative, the TypeScript types are derived).
 */

export type HealthStatus = "available" | "limited" | "unavailable";

export type Kind = "event" | "request" | "response" | "error" | "stream";

export interface PmFocusEntry {
  id: string;
  kind: string;
  title: string;
  status: string;
  priority?: string | null;
  reason?: string;
}

export interface PreflightItem {
  name: string;
  status: string;
  detail?: string;
}

export interface RlVisionDetection {
  entity: string;
  confidence: number;
  x: number;
  y: number;
  w: number;
  h: number;
  team?: string | null;
}

export interface Source {
  role: "core" | "shell" | "worker" | "plugin" | "pet" | "dashboard" | "supervisor" | "remote";
  id: string;
}

export interface Envelope {
  v?: number;
  id?: string;
  ts?: string;
  kind: Kind;
  name: string;
  corr?: string | null;
  src: Source;
  payload?: Record<string, unknown>;
}

export interface AuthRequest {
  token: string;
  role: "core" | "shell" | "worker" | "plugin" | "pet" | "dashboard" | "supervisor" | "remote";
  id: string;
  client_version?: string;
}

export interface AuthResponse {
  ok: boolean;
  session_id: string;
  core_version: string;
  schema_version?: number;
  reason?: string;
}

export interface ErrorPayload {
  code: string;
  message: string;
  retryable?: boolean;
  details?: Record<string, unknown>;
}

export interface ChatStreamFrame {
  delta: string;
  done?: boolean;
}

export interface ChatSendResult {
  request_id: string;
  text: string;
  provider: string;
  degraded: boolean;
}

export interface StreamPluginStatus {
  obs?: string;
  twitch?: string;
}

export interface StreamSessionStatus {
  active: boolean;
  session_id?: string | null;
  started_at?: string | null;
  scene?: string | null;
  plugins?: StreamPluginStatus;
}

export interface FunkenTopEntry {
  viewer_id: string;
  display_name?: string;
  balance: number;
  tier?: string;
}

export interface FunkenTop {
  viewers?: FunkenTopEntry[];
}

export interface RemoteDevice {
  id: string;
  name?: string;
  channel?: string;
  paired_at: string;
  last_seen_at?: string | null;
  revoked_at?: string | null;
  revoked_reason?: string;
}

export interface RemoteDevices {
  devices?: RemoteDevice[];
  enabled?: boolean;
}

export interface RemotePairCode {
  pairing_id: string;
  code: string;
  expires_at: string;
  ttl_s: number;
}

export interface RemoteUnpairResult {
  ok: boolean;
  device_id?: string;
}

export interface HealthHistoryEntry {
  id: number;
  ts: string;
  component: string;
  status: string;
  reason?: string;
}

export interface HealthHistoryResult {
  entries?: HealthHistoryEntry[];
}

export interface ConfigEffective {
  config?: Record<string, unknown>;
  schema_version?: number;
}

export interface ClipRecord {
  id: string;
  source: string;
  trigger_kind: string;
  origin_event_id?: string;
  session_id?: string;
  file_path: string;
  duration_s?: number;
  created_at: string;
  thumbnail_path?: string | null;
  tags?: string[];
  status?: string;
  parent_clip_id?: string | null;
  checksum?: string;
  notes?: string;
}

export interface ClipListResult {
  clips?: ClipRecord[];
}

export interface ClipTagResult {
  clip: ClipRecord;
}

export interface ClipExportResult {
  ok: boolean;
  export_path?: string | null;
  reason?: string;
}

export interface ClipTrimResult {
  ok: boolean;
  clip_id?: string | null;
  reason?: string;
}

export interface ConfigFieldSchema {
  path: string;
  type: string;
  options?: string[] | null;
  min?: number | null;
  max?: number | null;
  restart_required: boolean;
  group: string;
}

export interface ConfigSnapshot {
  values?: Record<string, unknown>;
  schema?: ConfigFieldSchema[];
  user_config_path?: string;
}

export interface ConfigSetResult {
  ok: boolean;
  applied?: string[];
  restart_required?: string[];
  errors?: Record<string, string>;
}

export interface SecretStatus {
  name: string;
  present: boolean;
  group: string;
}

export interface SecretsStatus {
  secrets?: SecretStatus[];
}

export interface PinStatus {
  configured?: boolean;
}

export interface SettingsOk {
  ok?: boolean;
}

export interface TwitchDeviceCode {
  user_code: string;
  verification_uri: string;
  expires_in: number;
  interval: number;
}

export interface TwitchAuthStatus {
  state: string;
  login?: string | null;
  expires_at?: string | null;
  scopes?: string[] | null;
  error?: string | null;
}

export interface PersonalityText {
  text: string;
  path: string;
}

export interface HealthChanged {
  component: string;
  status: HealthStatus;
  reason?: string;
}

export interface ModeChanged {
  previous: string;
  current: string;
  layers?: string[];
  reason?: string;
  game_running?: boolean;
}

export interface StateChanged {
  path: string;
  old?: unknown;
  new?: unknown;
  version: number;
}

export interface TranscriptReady {
  text: string;
  language: string;
  confidence: number;
  addressed_to_nox: boolean;
  duration_ms: number;
  latency_ms: number;
}

export interface VoiceKillPhrase {
  by?: string;
  reason?: string;
  language?: string;
}

export interface TtsStarted {
  text: string;
  channel: string;
  engine: string;
  utterance_id: string;
}

export interface AiRequestStarted {
  request_id: string;
  provider: string;
  role: string;
  mode: string;
}

export interface AiResponseReady {
  request_id: string;
  provider: string;
  text: string;
  latency_ms: number;
  tokens_in?: number | null;
  tokens_out?: number | null;
  degraded?: boolean;
  degraded_reason?: string;
}

export interface AiRequestFailed {
  request_id: string;
  provider: string;
  error: string;
  fallback_to?: string | null;
}

export interface PetStateChanged {
  functional: string;
  mood?: Record<string, number>;
  expression?: string;
  intensity?: number;
}

export interface PrivacyModeChanged {
  previous: string;
  current: string;
  by: string;
}

export interface CaptureChanged {
  microphone: boolean;
  camera: boolean;
  screen: boolean;
  cloud: boolean;
}

export interface KillSwitch {
  by: string;
  reason?: string;
}

export interface PermissionRequested {
  request_id: string;
  agent: string;
  tool: string;
  action: string;
  mode: string;
  risk: string;
  target?: string;
}

export interface PermissionDecided {
  request_id: string;
  decision: string;
  rule_id: string;
  by: string;
  reason?: string;
}

export interface AuditEntry {
  seq: number;
  actor: string;
  tool: string;
  action: string;
  target?: string;
  decision: string;
  result: string;
  task_id?: string | null;
  prev_hash: string;
  hash: string;
}

export interface PluginLifecycle {
  plugin_id: string;
  version: string;
  reason?: string;
}

export interface HealthReport {
  components: Record<string, HealthChanged>;
  generated_at?: string;
}

export interface StreamStarted {
  session_id: string;
  mode: string;
  obs_connected?: boolean;
  twitch_connected?: boolean;
}

export interface StreamEnded {
  session_id: string;
  duration_s: number;
  summary?: string;
  ended_reason?: string;
}

export interface StreamModeChanged {
  previous: string;
  current: string;
  by?: string;
}

export interface StreamPreflightResult {
  session_id: string;
  items?: PreflightItem[];
  overall?: string;
}

export interface StreamViewerSeen {
  viewer_id: string;
  first_time?: boolean;
}

export interface StreamFunkenAwarded {
  viewer_id: string;
  delta: number;
  reason?: string;
  balance_after?: number;
}

export interface StreamFunkenChanged {
  viewer_id: string;
  delta: number;
  reason?: string;
  balance_after: number;
  source: string;
  tier?: string;
}

export interface StreamMinigameStarted {
  session_id: string;
  game_id: string;
  participants?: string[];
}

export interface StreamMinigameEnded {
  session_id: string;
  game_id: string;
  participants?: string[];
  result?: Record<string, unknown>;
}

export interface ObsConnectionChanged {
  reason?: string;
}

export interface ObsDisconnected {
  reason?: string;
  backoff_s?: number;
}

export interface ObsSceneChanged {
  previous_scene?: string;
  current_scene: string;
  by?: string;
}

export interface ObsHealthChanged {
  mic_level_ok?: boolean;
  camera_ok?: boolean;
  render_lag_pct?: number;
  dropped_frames_pct?: number;
}

export interface ObsCrashDetected {
  detected_at?: string;
}

export interface ObsAutoRestarted {
  attempt: number;
  succeeded: boolean;
}

export interface TwitchConnectionChanged {
  reason?: string;
}

export interface TwitchDisconnected {
  reason?: string;
  backoff_s?: number;
}

export interface TwitchResynced {
  messages_recovered?: number;
  messages_lost_estimate?: number;
  gap_s?: number;
}

export interface TwitchChatMessage {
  chat_event_id: number;
  viewer_id?: string;
  text: string;
  channel?: string;
  relevance?: number;
  addressed_to_nox?: boolean;
}

export interface TwitchCommandInvoked {
  chat_event_id: number;
  viewer_id?: string;
  command: string;
  args?: string[];
}

export interface TwitchEvent {
  chat_event_id: number;
  kind: string;
  viewer_id?: string;
  priority?: number;
  payload?: Record<string, unknown>;
}

export interface TwitchChatMoodChanged {
  category: string;
  window?: string;
}

export interface TwitchModerationAction {
  chat_event_id: number;
  viewer_id?: string;
  stage: string;
  decision?: string;
  hard_list_hit?: boolean;
}

export interface GameDetected {
  game: string;
  detected_at?: string;
}

export interface GameEnded {
  game: string;
  duration_s?: number;
}

export interface RlMatchStarted {
  match_id: number;
  started_at?: string;
}

export interface RlMatchEnded {
  match_id: number;
  duration_s?: number;
  result?: string;
  summary_short?: string;
}

export interface RlEvent {
  match_id?: number | null;
  kind: string;
  source: string;
  confidence?: number;
  payload?: Record<string, unknown>;
}

export interface RlReplayParsed {
  replay_id?: number;
  parse_status?: string;
  matched_match_id?: number | null;
  file_path?: string;
  header?: Record<string, unknown>;
  parser_version?: string;
}

export interface RlCallout {
  match_id?: number | null;
  clip_id: string;
  rule_id: string;
  latency_ms?: number;
}

export interface RemoteMessage {
  channel?: string;
  sender_id: string;
  chat_id?: string;
  update_id?: number;
  text?: string;
}

export interface RemotePairingStarted {
  pairing_id: string;
  expires_at: string;
}

export interface RemotePaired {
  device_id: string;
  name?: string;
  channel?: string;
}

export interface RemoteRevoked {
  device_id: string;
  reason?: string;
}

export interface RemoteCommand {
  channel?: string;
  sender_id: string;
  device_id?: string;
  command: string;
  allowed: boolean;
  reason?: string;
}

export interface RemoteNotificationSent {
  event: string;
  sent: boolean;
  reason?: string;
}

export interface ClipRequested {
  trigger_kind: string;
  source: string;
  origin_event_id?: string;
  session_id?: string;
  tags?: string[];
}

export interface ClipSaved {
  clip_id: string;
  file_path: string;
  trigger_kind: string;
  source: string;
  duration_s?: number;
  tags?: string[];
}

export interface ClipFailed {
  trigger_kind: string;
  source: string;
  reason: string;
}

export interface ClipExported {
  clip_id: string;
  export_path: string;
}

export interface SensorForegroundChanged {
  process: string;
  title?: string;
  ts?: string;
}

export interface CreativeAppDetected {
  app: string;
  window_title?: string;
}

export interface CreativeAppLeft {
  app: string;
}

export interface CreativeNoteWritten {
  project: string;
  path: string;
}

export interface CreativeScreenshotRequested {
  corr: string;
  app: string;
  window_title?: string;
}

export interface CreativeScreenshotResult {
  corr: string;
  status: string;
  reason?: string;
  path?: string;
}

export interface PmItemChanged {
  id: string;
  kind: string;
  status: string;
  epic_id?: string | null;
  project_id?: string | null;
}

export interface PmFocusChanged {
  items?: PmFocusEntry[];
}

export interface PrivacyZoneChanged {
  active: boolean;
  zone?: string | null;
}

export interface SensorProcessStarted {
  process: string;
  pid?: number;
}

export interface SensorProcessEnded {
  process: string;
  pid?: number;
  duration_s?: number;
}

export interface CodingSessionStarted {
  session_id: string;
  story_id?: string;
  project_id?: string;
  workspace?: string;
}

export interface CodingSessionProgress {
  session_id: string;
  stage: string;
  detail?: string;
  tool_name?: string;
  files?: string[];
}

export interface CodingSessionEnded {
  session_id: string;
  outcome?: string;
  summary?: string;
  repair_attempts?: number;
}

export interface CodingSessionFailed {
  session_id: string;
  reason: string;
  repair_attempts?: number;
  detail?: string;
}

export interface ProactiveNotification {
  kind: string;
  priority: string;
  text: string;
  channel: string;
  spoken?: boolean;
  announced?: boolean;
}

export interface ProactiveSuppressed {
  kind: string;
  priority: string;
  reason: string;
}

export interface MemoryCreated {
  id: number;
  type: string;
  importance: number;
  source?: string;
  vault_path?: string | null;
}

export interface MemoryDeleted {
  id: number;
  reason?: string;
}

export interface MemoryIndexUpdated {
  path: string;
  chunks?: number;
}

export interface SettingsChanged {
  paths?: string[];
}

export interface TwitchAuthChanged {
  state: string;
  login?: string;
}

export interface RlVisionDetections {
  match_id?: number | null;
  backend?: string;
  detections?: RlVisionDetection[];
}

export interface RlVisionDisabled {
  reason: string;
  automatic?: boolean;
}

export interface RlVisionAnalysis {
  match_id?: number | null;
  frames_analyzed?: number;
  ball_side_ratio?: number | null;
  avg_self_car_ball_distance?: number | null;
  coaching_summary?: string;
}
