/**
 * The jaw in speech pauses: `tts.level` arrives about 30 times a second while a voice is heard and
 * only sparsely in a pause (one `0`, then now and then the output's noise floor). The jaw must shut
 * in a pause and stay shut until the voice is back - no fluttering, and no synthetic syllables
 * flapping in because the measured signal went quiet.
 *
 * Time is passed in, so every run is the same: a frame loop at 60 fps over a script of level
 * events, with the tracker feeding `SpeechEnvelope` exactly as `ReliefPet` does.
 */

import { describe, expect, it } from 'vitest';

import {
  FULL_OPEN_RMS,
  SpeechEnvelope,
  VOICE_HOLD_MS,
  VOICE_OPEN_RMS,
  VOICE_SILENT_RMS,
  VoiceTracker,
} from '../relief/speech';

const FRAME_MS = 1000 / 60;
/** The synthetic level the brain would use while speaking (`stateMapping`: at least 0.35). */
const SYNTHETIC_LEVEL = 0.5;

type LevelEvent = readonly [atMs: number, level: number];

interface Frame {
  ms: number;
  /** What the tracker hands the brain: a level, or null for "synthesise". */
  voice: number | null;
  /** The jaw opening 0..1 after the envelope. */
  jaw: number;
}

/**
 * Plays `events` (sorted by time) against a 60 fps frame loop up to `untilMs`. Like the reducer,
 * each event bumps `voiceSeq` and sets `voiceLevel`; like the brain, a null voice falls back to the
 * synthetic syllables.
 */
function play(events: readonly LevelEvent[], untilMs: number, speaking = true): Frame[] {
  const tracker = new VoiceTracker();
  const envelope = new SpeechEnvelope(3);
  let seq = 0;
  let level = 0;
  let next = 0;
  // The view starts watching before any level: its first look is the initial state.
  tracker.sample(0, seq, level, speaking);
  const frames: Frame[] = [];
  for (let ms = FRAME_MS; ms <= untilMs; ms += FRAME_MS) {
    while (next < events.length && events[next][0] <= ms) {
      level = events[next][1];
      seq += 1;
      next += 1;
    }
    const voice = tracker.sample(ms, seq, level, speaking);
    const jaw = envelope.sample(ms / 1000, FRAME_MS / 1000, SYNTHETIC_LEVEL, voice ?? undefined);
    frames.push({ ms, voice, jaw });
  }
  return frames;
}

/** Levels every 33 ms from `fromMs` to `toMs`, shaped by `shape(ms)`. */
function speech(fromMs: number, toMs: number, shape: (ms: number) => number): LevelEvent[] {
  const out: LevelEvent[] = [];
  for (let ms = fromMs; ms < toMs; ms += 33) out.push([ms, shape(ms)]);
  return out;
}

/** Enough for the envelope's release to have settled after the voice went quiet. */
const SETTLE_MS = 250;

/** A voiced stretch: syllables at ~5 per second between 0.05 and 0.2 RMS. */
const voiced = (ms: number): number => 0.125 + 0.075 * Math.sin((ms / 1000) * 2 * Math.PI * 5);

const between = (frames: Frame[], fromMs: number, toMs: number): Frame[] =>
  frames.filter((f) => f.ms >= fromMs && f.ms < toMs);

/** How often the jaw visibly opens again (crosses from shut to open) within `frames`. */
function openings(frames: Frame[]): number {
  let count = 0;
  let open = frames.length > 0 && frames[0].jaw > 0.05;
  for (const f of frames) {
    if (!open && f.jaw > 0.1) {
      count += 1;
      open = true;
    } else if (open && f.jaw < 0.05) {
      open = false;
    }
  }
  return count;
}

describe('the jaw in speech pauses', () => {
  it('eases shut when the levels stop for longer than the hold, and stays shut', () => {
    const frames = play(speech(0, 600, voiced), 1600);
    const pause = between(frames, 600 + VOICE_HOLD_MS + SETTLE_MS, 1600);
    expect(pause.length).toBeGreaterThan(0);
    for (const f of pause) {
      expect(f.voice).toBe(0); // measured silence, not "synthesise"
      expect(f.jaw).toBeLessThan(0.02);
    }
    // closing is a movement, not a snap: no single frame drops it by more than half its range
    const closing = between(frames, 600 - FRAME_MS, 600 + VOICE_HOLD_MS + SETTLE_MS);
    for (let i = 1; i < closing.length; i++) {
      expect(closing[i - 1].jaw - closing[i].jaw).toBeLessThan(0.5);
    }
  });

  it('never flaps the synthetic syllables in during a pause of the measured voice', () => {
    // One second of silence: the core sent its one `0` and nothing after.
    const frames = play([...speech(0, 400, voiced), [400, 0], ...speech(1400, 1800, voiced)], 1800);
    const pause = between(frames, 400, 1400);
    expect(pause.every((f) => f.voice === 0)).toBe(true);
    expect(openings(pause)).toBe(0);
    expect(Math.max(...between(frames, 400 + SETTLE_MS, 1400).map((f) => f.jaw))).toBeLessThan(0.02);
  });

  it('keeps the jaw shut through sparse keepalives below the opening threshold', () => {
    const quiet = VOICE_OPEN_RMS * 0.9;
    const keepalives: LevelEvent[] = [
      [400, 0],
      [650, quiet],
      [700, 0.004],
      [950, quiet],
      [1200, 0.012],
      [1210, 0.018],
    ];
    const frames = play([...speech(0, 400, voiced), ...keepalives], 1500);
    const pause = between(frames, 400, 1500);
    expect(pause.every((f) => f.voice === 0)).toBe(true);
    expect(openings(pause)).toBe(0);
    expect(Math.max(...between(frames, 400 + SETTLE_MS, 1500).map((f) => f.jaw))).toBeLessThan(0.02);
  });

  it('opens again as soon as a real level arrives after the pause', () => {
    const frames = play([...speech(0, 400, voiced), [400, 0], [1000, FULL_OPEN_RMS]], 1100);
    const before = between(frames, 900, 1000);
    expect(Math.max(...before.map((f) => f.jaw))).toBeLessThan(0.02);
    const after = between(frames, 1000, 1000 + 100);
    expect(after[0].voice).toBeCloseTo(FULL_OPEN_RMS);
    expect(Math.max(...after.map((f) => f.jaw))).toBeGreaterThan(0.9); // within 100 ms, wide open
  });

  it('follows a quiet tail once open, down to the silence threshold (hysteresis)', () => {
    const tracker = new VoiceTracker();
    tracker.sample(0, 0, 0, true);
    const tail = (VOICE_OPEN_RMS + VOICE_SILENT_RMS) / 2;
    expect(tracker.sample(10, 1, tail, true)).toBe(0); // too quiet to open a shut jaw
    expect(tracker.sample(20, 2, 0.1, true)).toBeCloseTo(0.1);
    expect(tracker.sample(30, 3, tail, true)).toBeCloseTo(tail); // an open jaw follows it down
    expect(tracker.sample(40, 4, VOICE_SILENT_RMS / 2, true)).toBe(0);
    expect(tracker.sample(50, 5, tail, true)).toBe(0); // and stays shut for it again
  });
});

describe('the jaw while speaking', () => {
  it('follows the measured level smoothly', () => {
    const frames = between(play(speech(0, 1000, voiced), 1000), 100, 1000);
    const peak = Math.max(...frames.map((f) => f.jaw));
    const trough = Math.min(...frames.map((f) => f.jaw));
    expect(peak).toBeGreaterThan(0.8); // the loud syllables open it wide
    expect(trough).toBeGreaterThan(0.2); // and it is never shut mid-word
    for (let i = 1; i < frames.length; i++) {
      expect(Math.abs(frames[i].jaw - frames[i - 1].jaw)).toBeLessThan(0.25);
    }
    // a 5 Hz voice is five syllables a second: the jaw moves with it, it does not just hang open
    let turns = 0;
    for (let i = 2; i < frames.length; i++) {
      const a = frames[i - 1].jaw - frames[i - 2].jaw;
      const b = frames[i].jaw - frames[i - 1].jaw;
      if (a > 0.002 && b < -0.002) turns += 1;
    }
    expect(turns).toBeGreaterThanOrEqual(4);
  });

  it('ignores the level that was there before the view started watching', () => {
    const tracker = new VoiceTracker();
    expect(tracker.sample(0, 7, 0.2, true)).toBeNull();
    expect(tracker.sample(50, 7, 0.2, true)).toBeNull();
  });

  it('hands back to the synthetic syllables once the utterance is over', () => {
    const tracker = new VoiceTracker();
    tracker.sample(0, 0, 0, true);
    expect(tracker.sample(10, 1, 0.1, true)).toBeCloseTo(0.1);
    expect(tracker.sample(20, 1, 0.1, false)).toBeNull(); // the utterance ended
    // the next one comes from somewhere that sends no levels: synthesise, do not sit shut
    expect(tracker.sample(2000, 1, 0.1, true)).toBeNull();
    expect(tracker.sample(2010, 2, 0.1, true)).toBeCloseTo(0.1); // until a level arrives
  });

  it('has no voice at all without levels', () => {
    const frames = play([], 500, true);
    expect(frames.every((f) => f.voice === null)).toBe(true);
  });
});
