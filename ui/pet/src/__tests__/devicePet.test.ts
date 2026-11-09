/**
 * The pet of one paired node (C9): an overlay a node relays carries `?device=<id>` and follows
 * that node's speech only; the core's own pet ignores a node's speech. Nothing else is filtered.
 */

import { describe, expect, it } from 'vitest';

import type { Envelope } from '../../../shared/envelope';
import { deviceFromQuery, forThisPet } from '../ipc';
import { INITIAL_STATE, reduceEvent } from '../petState';

const NODE = 'a'.repeat(32);
const OTHER = 'b'.repeat(32);

function event(name: string, payload: Record<string, unknown>): Envelope {
  return { name, payload } as unknown as Envelope;
}

describe('deviceFromQuery', () => {
  it('takes a node id and nothing else', () => {
    expect(deviceFromQuery(NODE)).toBe(NODE);
    expect(deviceFromQuery(null)).toBeNull();
    expect(deviceFromQuery('Living room Pi')).toBeNull();
    expect(deviceFromQuery(`${NODE}x`)).toBeNull();
    expect(deviceFromQuery('A'.repeat(32))).toBeNull();
  });
});

describe('forThisPet', () => {
  const nodeLevel = event('tts.level', { level: 0.5, device: NODE, utterance_id: 'u', channel: 'private' });
  const coreLevel = event('tts.level', { level: 0.5, utterance_id: 'u', channel: 'private' });

  it("lets a node's pet follow that node's speech only", () => {
    expect(forThisPet(nodeLevel, NODE)).toBe(true);
    expect(forThisPet(event('tts.level', { level: 0.5, device: OTHER }), NODE)).toBe(false);
    expect(forThisPet(coreLevel, NODE)).toBe(false);
  });

  it("keeps the core's pet on the core's speech", () => {
    expect(forThisPet(coreLevel, null)).toBe(true);
    expect(forThisPet(nodeLevel, null)).toBe(false);
  });

  it('never filters anything but speech', () => {
    for (const name of ['privacy.mode_changed', 'system.started', 'pet.mood', 'state.changed']) {
      expect(forThisPet(event(name, { device: OTHER }), NODE)).toBe(true);
      expect(forThisPet(event(name, {}), null)).toBe(true);
    }
  });

  it("moves the node pet's mouth with a node level, as the core pet's with its own", () => {
    const next = reduceEvent(INITIAL_STATE, 'tts.level', nodeLevel.payload);
    expect(next.voiceLevel).toBe(0.5);
    expect(next.voiceSeq).toBe(INITIAL_STATE.voiceSeq + 1);
  });
});
