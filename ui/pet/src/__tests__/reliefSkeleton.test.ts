/**
 * The relief skeleton: hierarchy maths and the spring ("jiggle") bones that give ears, cheek fur,
 * wings and tails their follow-through. Behavioural: what a viewer would see, not how it is coded.
 */

import { describe, expect, it } from 'vitest';

import { apply } from '../relief/math';
import { type BoneDef, Skeleton, SkeletonError } from '../relief/skeleton';

const J = { stiffness: 260, damping: 9, limit: 0.5, gravity: 0 };
const BONES: BoneDef[] = [
  { name: 'root', parent: null, pivot: [0, 0], tip: [0, -50] },
  { name: 'arm', parent: 'root', pivot: [0, -50], tip: [0, -100] },
  { name: 'ear', parent: 'arm', pivot: [0, -100], tip: [0, -140], jiggle: J },
];

describe('relief skeleton hierarchy', () => {
  it('carries a child along when its parent turns', () => {
    const s = new Skeleton(BONES);
    s.get('root').rot = Math.PI / 2;
    s.update(0);
    const [x, y] = apply(s.world[s.index.get('arm')!], 0, -100);
    // a quarter turn about the root pivot moves the arm tip from straight up to the side
    expect(x).toBeCloseTo(100, 6);
    expect(y).toBeCloseTo(0, 6);
  });

  it('rotates a bone about its own pivot, not the origin', () => {
    const s = new Skeleton(BONES);
    s.get('arm').rot = Math.PI;
    s.update(0);
    const [x, y] = apply(s.world[s.index.get('arm')!], 0, -50);
    expect(x).toBeCloseTo(0, 6);
    expect(y).toBeCloseTo(-50, 6);
  });

  it('refuses a bone listed before its parent, an unknown parent and a second root', () => {
    expect(() => new Skeleton([BONES[1], BONES[0]])).toThrow(SkeletonError);
    expect(() => new Skeleton([BONES[0], { ...BONES[1], parent: 'nope' }])).toThrow(SkeletonError);
    expect(() => new Skeleton([BONES[0], { ...BONES[1], parent: null }])).toThrow(SkeletonError);
  });
});

describe('relief spring bones', () => {
  function swing(frames: number, dt = 1 / 60): { offsets: number[]; s: Skeleton } {
    const s = new Skeleton(BONES);
    s.update(0);
    const offsets: number[] = [];
    for (let i = 0; i < frames; i++) {
      s.resetPose();
      // the arm snaps sideways for 10 frames, then holds still
      s.get('arm').rot = i < 10 ? (i / 10) * 0.6 : 0.6;
      s.update(dt);
      offsets.push(s.springOffset('ear'));
    }
    return { offsets, s };
  }

  it('lags behind a sudden turn and then settles on the pose', () => {
    const { offsets } = swing(120);
    // the ear trails the arm while it turns: its spring angle points against the turn
    expect(Math.min(...offsets.slice(0, 12))).toBeLessThan(-0.05);
    // and a second later it rests where the animation put it
    expect(Math.abs(offsets[offsets.length - 1])).toBeLessThan(0.01);
  });

  it('overshoots once (follow-through) rather than creeping in', () => {
    const { offsets } = swing(120);
    expect(Math.max(...offsets.slice(10))).toBeGreaterThan(0.005);
  });

  it('never bends further than its limit', () => {
    const s = new Skeleton(BONES);
    s.update(0);
    for (let i = 0; i < 60; i++) {
      s.resetPose();
      s.get('root').rot = i % 2 ? 1.5 : -1.5; // violent shaking
      s.update(1 / 60);
      expect(Math.abs(s.springOffset('ear'))).toBeLessThanOrEqual(J.limit + 1e-9);
    }
  });

  it('starts at rest after a reset or a zero-length frame', () => {
    const { s } = swing(5);
    s.update(0);
    expect(s.springOffset('ear')).toBe(0);
    s.resetPhysics();
    s.update(1 / 60);
    expect(s.springOffset('ear')).toBe(0);
  });

  it('stays finite after a stalled tab hands it a huge frame', () => {
    const s = new Skeleton(BONES);
    s.update(0);
    s.get('arm').rot = 1;
    s.update(30);
    expect(Number.isFinite(s.springOffset('ear'))).toBe(true);
    expect(s.world.every((m) => m.every(Number.isFinite))).toBe(true);
  });
});
