/**
 * Presets as the dashboard sees them.
 *
 * The wire shape is the configuration shape - `presets.list` returns the validated section - so
 * these types mirror `nox.core.config.presets` rather than inventing a view model. Parsing is
 * defensive in one direction only: a payload that is missing or malformed yields an empty page
 * with nothing selected, never a half-built preset that looks editable and is not.
 */

export type StepKind = 'light' | 'switch' | 'scene' | 'climate' | 'mode' | 'run' | 'say' | 'wait';

export const STEP_KINDS: StepKind[] = [
  'light',
  'switch',
  'scene',
  'climate',
  'mode',
  'run',
  'say',
  'wait',
];

/** Every field any step can carry. Which ones apply is decided by `kind`. */
export interface PresetStep {
  kind: StepKind;
  entity_ids?: string[];
  entity_id?: string;
  on?: boolean | null;
  brightness_pct?: number | null;
  color_temp_kelvin?: number | null;
  temperature_c?: number;
  mode?: string;
  action?: string;
  text?: string;
  seconds?: number;
}

export interface PresetTriggers {
  phrases: string[];
  at: string;
  days: string[];
  on_process_start: string;
  on_process_end: string;
}

export interface Preset {
  id: string;
  name: string;
  enabled: boolean;
  triggers: PresetTriggers;
  steps: PresetStep[];
}

export interface PresetAction {
  id: string;
  name: string;
  command: string[];
  working_dir: string;
  timeout_s: number;
  report_failure: boolean;
}

export interface PresetsPayload {
  enabled: boolean;
  presets: Preset[];
  actions: PresetAction[];
}

/** One step of a finished run, as `presets.activate` reports it. */
export interface RunStepResult {
  index: number;
  kind: string;
  ok: boolean;
  error: string;
  duration_ms: number;
}

export interface PresetRunResult {
  preset_id: string;
  name: string;
  trigger: string;
  ok: boolean;
  steps: RunStepResult[];
  duration_ms: number;
}

/** What `presets.test_action` reports about one program start. */
export interface ActionTestResult {
  ok: boolean;
  exit_code: number | null;
  error: string;
  duration_ms: number;
  timed_out: boolean;
}

const EMPTY_TRIGGERS: PresetTriggers = {
  phrases: [],
  at: '',
  days: [],
  on_process_start: '',
  on_process_end: '',
};

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === 'string') : [];
}

function parseTriggers(value: unknown): PresetTriggers {
  const raw = asRecord(value);
  return {
    phrases: asStrings(raw.phrases),
    at: typeof raw.at === 'string' ? raw.at : '',
    days: asStrings(raw.days),
    on_process_start: typeof raw.on_process_start === 'string' ? raw.on_process_start : '',
    on_process_end: typeof raw.on_process_end === 'string' ? raw.on_process_end : '',
  };
}

function parseSteps(value: unknown): PresetStep[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((entry) => asRecord(entry))
    .filter((raw): raw is Record<string, unknown> => STEP_KINDS.includes(raw.kind as StepKind))
    .map((raw) => raw as unknown as PresetStep);
}

export function parsePresets(payload: unknown): PresetsPayload {
  const raw = asRecord(payload);
  const presets = Array.isArray(raw.presets) ? raw.presets : [];
  const actions = Array.isArray(raw.actions) ? raw.actions : [];
  return {
    enabled: raw.enabled !== false,
    presets: presets.map((entry) => {
      const item = asRecord(entry);
      return {
        id: typeof item.id === 'string' ? item.id : '',
        name: typeof item.name === 'string' ? item.name : '',
        enabled: item.enabled !== false,
        triggers: item.triggers ? parseTriggers(item.triggers) : { ...EMPTY_TRIGGERS },
        steps: parseSteps(item.steps),
      };
    }),
    actions: actions.map((entry) => {
      const item = asRecord(entry);
      return {
        id: typeof item.id === 'string' ? item.id : '',
        name: typeof item.name === 'string' ? item.name : '',
        command: asStrings(item.command),
        working_dir: typeof item.working_dir === 'string' ? item.working_dir : '',
        timeout_s: typeof item.timeout_s === 'number' ? item.timeout_s : 20,
        report_failure: item.report_failure !== false,
      };
    }),
  };
}

export function parseRunResult(payload: unknown): PresetRunResult | null {
  const raw = asRecord(payload);
  if (typeof raw.preset_id !== 'string') return null;
  const steps = Array.isArray(raw.steps) ? raw.steps : [];
  return {
    preset_id: raw.preset_id,
    name: typeof raw.name === 'string' ? raw.name : raw.preset_id,
    trigger: typeof raw.trigger === 'string' ? raw.trigger : '',
    ok: raw.ok === true,
    duration_ms: typeof raw.duration_ms === 'number' ? raw.duration_ms : 0,
    steps: steps.map((entry) => {
      const item = asRecord(entry);
      return {
        index: typeof item.index === 'number' ? item.index : 0,
        kind: typeof item.kind === 'string' ? item.kind : '',
        ok: item.ok === true,
        error: typeof item.error === 'string' ? item.error : '',
        duration_ms: typeof item.duration_ms === 'number' ? item.duration_ms : 0,
      };
    }),
  };
}

export function parseActionTest(payload: unknown): ActionTestResult {
  const raw = asRecord(payload);
  return {
    ok: raw.ok === true,
    exit_code: typeof raw.exit_code === 'number' ? raw.exit_code : null,
    error: typeof raw.error === 'string' ? raw.error : '',
    duration_ms: typeof raw.duration_ms === 'number' ? raw.duration_ms : 0,
    timed_out: raw.timed_out === true,
  };
}

/** A new, valid-by-construction step of the given kind, so the editor never starts from nothing. */
export function blankStep(kind: StepKind): PresetStep {
  switch (kind) {
    case 'light':
      return { kind, entity_ids: [], on: true };
    case 'switch':
      return { kind, entity_ids: [], on: true };
    case 'scene':
      return { kind, entity_id: '' };
    case 'climate':
      return { kind, entity_ids: [], temperature_c: 21 };
    case 'mode':
      return { kind, mode: 'companion' };
    case 'run':
      return { kind, action: '' };
    case 'say':
      return { kind, text: '' };
    case 'wait':
      return { kind, seconds: 1 };
  }
}

export function blankPreset(id: string): Preset {
  return { id, name: '', enabled: true, triggers: { ...EMPTY_TRIGGERS }, steps: [] };
}

export function blankAction(id: string): PresetAction {
  return { id, name: '', command: [''], working_dir: '', timeout_s: 20, report_failure: true };
}

/** Turn a display name into a usable identifier (lower case, dashes, no punctuation). */
export function slugify(name: string): string {
  const base = name
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40);
  return base || 'preset';
}
