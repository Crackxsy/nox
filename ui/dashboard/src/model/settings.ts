/**
 * Editable settings: `config.*`, `secrets.*`, `security.pin.*`, `twitch.auth.*`, `personality.*`.
 *
 * Two rules shape everything here. A setting whose type this UI cannot render is *dropped* rather
 * than guessed into a text field, and a secret's value never exists on this side at all — only the
 * fact that a name is set.
 */

import type {
  ConfigFieldSchema as WireConfigFieldSchema,
  ConfigSetResult as WireConfigSetResult,
  SecretStatus as WireSecretStatus,
  TwitchAuthStatus as WireTwitchAuthStatus,
  TwitchDeviceCode as WireTwitchDeviceCode,
} from '../../../shared/generated/ipc';
import { bool, isRecord, numOrNull, str, stringList } from '../../../shared/guards';
import type { FieldMap } from './wire';

export type SettingType = 'string' | 'int' | 'float' | 'bool' | 'enum' | 'list[str]';

const SETTING_TYPES: readonly string[] = ['string', 'int', 'float', 'bool', 'enum', 'list[str]'];

export interface SettingSpec {
  path: string;
  type: SettingType;
  options: string[];
  min: number | null;
  max: number | null;
  restartRequired: boolean;
  group: string;
}

/** See `model/wire.ts`. */
export const SETTING_FIELDS: FieldMap<WireConfigFieldSchema, SettingSpec> = {
  path: 'path',
  type: 'type',
  options: 'options',
  min: 'min',
  max: 'max',
  restart_required: 'restartRequired',
  group: 'group',
};

export interface EditableConfig {
  values: Record<string, unknown>;
  schema: SettingSpec[];
  userConfigPath: string;
}

/**
 * `config.get {}` response: `{values, schema, user_config_path}`. An entry without a usable `path`
 * or with a type this UI cannot render is dropped rather than guessed into a text field.
 */
export function parseEditableConfig(payload: unknown): EditableConfig | null {
  if (!isRecord(payload)) return null;
  const rawSchema = payload.schema;
  if (!Array.isArray(rawSchema)) return null;
  const schema: SettingSpec[] = [];
  for (const entry of rawSchema) {
    if (!isRecord(entry)) continue;
    const path = str(entry.path);
    const type = str(entry.type);
    if (!path || !SETTING_TYPES.includes(type)) continue;
    schema.push({
      path,
      type: type as SettingType,
      options: Array.isArray(entry.options)
        ? entry.options.map((o) => String(o)).filter((o) => o.length > 0)
        : [],
      min: numOrNull(entry.min),
      max: numOrNull(entry.max),
      restartRequired: bool(entry.restart_required),
      group: str(entry.group, 'identity'),
    });
  }
  return {
    values: isRecord(payload.values) ? payload.values : {},
    schema,
    userConfigPath: str(payload.user_config_path),
  };
}

export interface ConfigSetResult {
  ok: boolean;
  applied: string[];
  restartRequired: string[];
  errors: Record<string, string>;
}

/** See `model/wire.ts`. */
export const CONFIG_SET_FIELDS: FieldMap<WireConfigSetResult, ConfigSetResult> = {
  ok: 'ok',
  applied: 'applied',
  restart_required: 'restartRequired',
  errors: 'errors',
};

/** `config.set {values}` response: `{ok, applied, restart_required, errors}`. */
export function parseConfigSetResult(payload: unknown): ConfigSetResult {
  const rec = isRecord(payload) ? payload : {};
  const errors: Record<string, string> = {};
  if (isRecord(rec.errors)) {
    for (const [path, message] of Object.entries(rec.errors)) errors[path] = String(message);
  }
  return {
    ok: rec.ok !== false && Object.keys(errors).length === 0,
    applied: stringList(rec.applied),
    restartRequired: stringList(rec.restart_required),
    errors,
  };
}

export interface SecretStatus {
  name: string;
  present: boolean;
  group: string;
}

/** See `model/wire.ts`. */
export const SECRET_FIELDS: FieldMap<WireSecretStatus, SecretStatus> = {
  name: 'name',
  present: 'present',
  group: 'group',
};

/** `secrets.status {}` response: `{secrets: [{name, present, group}]}`. Values are never sent. */
export function parseSecretStatus(payload: unknown): SecretStatus[] | null {
  if (!isRecord(payload) || !Array.isArray(payload.secrets)) return null;
  const out: SecretStatus[] = [];
  for (const entry of payload.secrets) {
    if (!isRecord(entry)) continue;
    const name = str(entry.name);
    if (!name) continue;
    out.push({ name, present: bool(entry.present), group: str(entry.group) });
  }
  return out;
}

/**
 * `security.pin.status {}` response: `{configured}`. Anything else — a refused request, a core
 * that does not know the name — reads as `false`: the page then sends no PIN and the core answers
 * with its own "PIN required" error, which is better than this UI demanding a PIN nobody set.
 */
export function parsePinConfigured(payload: unknown): boolean {
  return isRecord(payload) && payload.configured === true;
}

/**
 * Which PIN message an error from `secrets.set`/`secrets.delete` deserves, if any.
 *
 * The core answers every refusal with the same code, so the message is the only thing separating
 * "no PIN was sent" from "that PIN was wrong" — and a permission error that has nothing to do with
 * the PIN (an unknown secret name) keeps its own message instead of being relabelled.
 */
export function pinErrorKey(
  code: string,
  message: string,
  pinSent: boolean,
): 'pin_required' | 'pin_wrong' | null {
  if (code !== 'permission.denied' || !/\bpin\b/i.test(message)) return null;
  return pinSent ? 'pin_wrong' : 'pin_required';
}

export interface TwitchDeviceCode {
  userCode: string;
  verificationUri: string;
  /** Seconds the code stays valid; the page stops polling when they are up. */
  expiresIn: number;
  /** Seconds between two `twitch.auth.status` polls, as dictated by the core. */
  interval: number;
}

/** See `model/wire.ts`. */
export const TWITCH_CODE_FIELDS: FieldMap<WireTwitchDeviceCode, TwitchDeviceCode> = {
  user_code: 'userCode',
  verification_uri: 'verificationUri',
  expires_in: 'expiresIn',
  interval: 'interval',
};

/** `twitch.auth.start {}` response. Returns null when no usable code came back. */
export function parseTwitchDeviceCode(payload: unknown): TwitchDeviceCode | null {
  if (!isRecord(payload)) return null;
  const userCode = str(payload.user_code);
  const verificationUri = str(payload.verification_uri);
  if (!userCode || !verificationUri) return null;
  return {
    userCode,
    verificationUri,
    expiresIn: numOrNull(payload.expires_in) ?? 0,
    interval: Math.max(1, numOrNull(payload.interval) ?? 5),
  };
}

export interface TwitchAuthStatus {
  /** idle | pending | authorized | expired | error — an unknown state is passed through verbatim. */
  state: string;
  login: string;
  expiresAt: string;
  scopes: string[];
  error: string;
}

/** See `model/wire.ts`. */
export const TWITCH_STATUS_FIELDS: FieldMap<WireTwitchAuthStatus, TwitchAuthStatus> = {
  state: 'state',
  login: 'login',
  expires_at: 'expiresAt',
  scopes: 'scopes',
  error: 'error',
};

/** `twitch.auth.status {}` response. */
export function parseTwitchAuthStatus(payload: unknown): TwitchAuthStatus | null {
  if (!isRecord(payload)) return null;
  const state = str(payload.state);
  if (!state) return null;
  return {
    state,
    login: str(payload.login),
    expiresAt: str(payload.expires_at),
    scopes: stringList(payload.scopes),
    error: str(payload.error),
  };
}

export interface Personality {
  text: string;
  path: string;
}

/** `personality.get {}` response: `{text, path}`. */
export function parsePersonality(payload: unknown): Personality | null {
  if (!isRecord(payload) || typeof payload.text !== 'string') return null;
  return { text: payload.text, path: str(payload.path) };
}
