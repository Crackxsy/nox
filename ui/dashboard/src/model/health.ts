/**
 * `health.get` / `health.report` / `health.history` — the system-health view model.
 *
 * A component the core did not describe is dropped, never defaulted to "available": the health
 * table is the one screen whose whole job is to be believed.
 */

import type { HealthHistoryEntry as WireHealthHistoryEntry } from '../../../shared/generated/ipc';
import { isRecord, numOrNull, str } from '../../../shared/guards';
import type { FieldMap } from './wire';

export type HealthStatus = 'available' | 'limited' | 'unavailable';

export interface HealthComponent {
  component: string;
  status: string;
  reason: string;
}

export interface HealthState {
  components: HealthComponent[];
  generatedAt: string | null;
}

export interface HealthHistoryEntry {
  id: number;
  ts: string;
  component: string;
  status: string;
  reason: string;
}

/** See `model/wire.ts`: renaming a field on either side breaks this table, and the build. */
export const HEALTH_HISTORY_FIELDS: FieldMap<WireHealthHistoryEntry, HealthHistoryEntry> = {
  id: 'id',
  ts: 'ts',
  component: 'component',
  status: 'status',
  reason: 'reason',
};

/** `health.get` response / `health.report` event → HealthReport (components: dict[str, HealthChanged]). */
export function parseHealthReport(payload: unknown): HealthState | null {
  if (!isRecord(payload)) return null;
  const raw = payload.components;
  if (!isRecord(raw)) return null;
  const components: HealthComponent[] = Object.entries(raw).map(([name, value]) => ({
    component: isRecord(value) ? str(value.component, name) : name,
    status: isRecord(value) ? str(value.status, 'unavailable') : 'unavailable',
    reason: isRecord(value) ? str(value.reason) : '',
  }));
  components.sort((a, b) => a.component.localeCompare(b.component));
  return {
    components,
    generatedAt: typeof payload.generated_at === 'string' ? payload.generated_at : null,
  };
}

/** `health.history {}` response (`nox.ipc.protocol.HealthHistoryResult`). */
export function parseHealthHistory(payload: unknown): HealthHistoryEntry[] {
  if (!isRecord(payload) || !Array.isArray(payload.entries)) return [];
  const out: HealthHistoryEntry[] = [];
  for (const entry of payload.entries) {
    if (!isRecord(entry)) continue;
    const id = numOrNull(entry.id);
    if (id === null) continue;
    out.push({
      id,
      ts: str(entry.ts),
      component: str(entry.component),
      status: str(entry.status, 'unavailable'),
      reason: str(entry.reason),
    });
  }
  return out;
}

/** One `system.health_changed` event folded into the component list, keeping it sorted. */
export function applyHealthChanged(
  current: HealthState | null,
  payload: Record<string, unknown>,
): HealthState | null {
  const component = str(payload.component);
  if (!component) return current;
  const entry: HealthComponent = {
    component,
    status: str(payload.status, 'unavailable'),
    reason: str(payload.reason),
  };
  const components = (current?.components ?? []).filter((c) => c.component !== component);
  components.push(entry);
  components.sort((a, b) => a.component.localeCompare(b.component));
  return { components, generatedAt: current?.generatedAt ?? null };
}
