/**
 * Touch targets on the Devices tab, as the stylesheet declares them (WCAG 2.2, 2.5.8: at least
 * 24 x 24 CSS px). jsdom does no layout, so this reads `styles.css` itself: for each control, the
 * rules that reach it at the click tests' widths (phone 390, tablet 834, desktop 1440 - see
 * `tests/e2e/test_ux_redesign_stubbed.py`), in cascade order, with the tokens resolved. A size that
 * depends on the font, on a word fitting, or on flex leaving room fails here: only declared minimums
 * and paddings count.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const STYLES = readFileSync(join(process.cwd(), 'src', 'styles.css'), 'utf8');
const TOKENS = readFileSync(join(process.cwd(), 'src', 'tokens.css'), 'utf8');

/** WCAG 2.2 target size minimum (2.5.8). */
const MIN_TARGET_PX = 24;
const WIDTHS = { phone: 390, tablet: 834, desktop: 1440 } as const;

interface Rule {
  media: string;
  selectors: string[];
  decls: [string, string][];
}

const strip = (css: string): string => css.replace(/\/\*[\s\S]*?\*\//g, '');

/** The index of the `}` that closes the block opened at `open`. */
function closing(css: string, open: number): number {
  let depth = 0;
  for (let i = open; i < css.length; i++) {
    if (css[i] === '{') depth++;
    if (css[i] === '}' && --depth === 0) return i;
  }
  throw new Error('unbalanced braces in styles.css');
}

function parse(css: string, media = ''): Rule[] {
  const rules: Rule[] = [];
  let i = 0;
  while (i < css.length) {
    const open = css.indexOf('{', i);
    if (open === -1) break;
    const prelude = css.slice(i, open).replace(/^[\s;]*(@import[^;]*;)?/, '').trim();
    const end = closing(css, open);
    const body = css.slice(open + 1, end);
    if (prelude.startsWith('@media')) {
      rules.push(...parse(body, prelude.slice('@media'.length).trim()));
    } else if (!prelude.startsWith('@')) {
      const decls = body
        .split(';')
        .map((d) => d.trim())
        .filter((d) => d.includes(':'))
        .map((d): [string, string] => [d.slice(0, d.indexOf(':')).trim(), d.slice(d.indexOf(':') + 1).trim()]);
      rules.push({ media, selectors: prelude.split(',').map((s) => s.trim()), decls });
    }
    i = end + 1;
  }
  return rules;
}

const RULES = parse(strip(STYLES));

/** Whether a media condition holds at `width`. Features other than width are assumed to hold. */
function mediaHolds(media: string, width: number): boolean {
  for (const [, kind, px] of media.matchAll(/\((max|min)-width:\s*(\d+)px\)/g)) {
    if (kind === 'max' && width > Number(px)) return false;
    if (kind === 'min' && width < Number(px)) return false;
  }
  return true;
}

/**
 * Whether `selector` reaches an element whose own classes are `self` (tag `tag`) inside ancestors
 * with `ancestors` classes. Ancestor tags are not modelled, so a part naming one is taken to match:
 * that keeps every rule that might apply. Interaction states (`:hover`, `:disabled` ...) are left
 * out: they are not the resting target.
 */
function reaches(selector: string, tag: string, self: string[], ancestors: string[]): boolean {
  if (selector.includes(':') || selector.includes('[')) return false;
  const parts = selector.split(/\s*[>+~]\s*|\s+/).filter(Boolean);
  const compound = (part: string, classes: string[], wantTag?: string): boolean => {
    const m = /^([a-z]*)((?:\.[\w-]+)*)$/.exec(part);
    if (!m) return false;
    if (wantTag !== undefined && m[1] && m[1] !== wantTag) return false;
    return m[2]!.split('.').filter(Boolean).every((c) => classes.includes(c));
  };
  const last = parts.pop();
  if (!last || !compound(last, self, tag)) return false;
  return parts.every((part) => compound(part, ancestors));
}

function tokens(): Record<string, string> {
  const root = TOKENS.slice(TOKENS.indexOf(':root {'));
  const block = root.slice(0, closing(root, root.indexOf('{')));
  const out: Record<string, string> = {};
  for (const [, name, value] of strip(block).matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) out[name!] = value!.trim();
  return out;
}
const TOKEN = tokens();

/** A length in px, tokens resolved; `null` for anything not a plain length (auto, %, calc ...). */
function px(value: string): number | null {
  const resolved = value.replace(/var\((--[a-z0-9-]+)\)/g, (_, name: string) => TOKEN[name] ?? 'unknown');
  if (/^0$/.test(resolved)) return 0;
  const m = /^(\d+(?:\.\d+)?)px$/.exec(resolved);
  return m ? Number(m[1]) : null;
}

/** The declarations that win for the element at `width` (source order; no `!important` here). */
function computed(width: number, tag: string, self: string[], ancestors: string[] = []): Map<string, string> {
  const out = new Map<string, string>();
  for (const rule of RULES) {
    if (!mediaHolds(rule.media, width)) continue;
    if (!rule.selectors.some((s) => reaches(s, tag, self, ancestors))) continue;
    for (const [prop, value] of rule.decls) {
      expect(value, `${prop} in ${rule.selectors.join(', ')}`).not.toContain('!important');
      // Physical and logical names are the same property in a horizontal writing mode.
      const name = { 'min-width': 'min-inline-size', 'min-height': 'min-block-size' }[prop] ?? prop;
      out.set(name, value);
    }
  }
  return out;
}

/** Horizontal padding (left + right) from the `padding` shorthand or its longhands. */
function inlinePadding(style: Map<string, string>): number {
  const parts = (style.get('padding') ?? '0').split(/\s+/).map((v) => px(v) ?? 0);
  const [, right = parts[0]!, , left = right] = parts;
  const l = style.has('padding-left') ? (px(style.get('padding-left')!) ?? 0) : left;
  const r = style.has('padding-right') ? (px(style.get('padding-right')!) ?? 0) : right;
  return l + r;
}

describe('touch targets on the Devices tab (WCAG 2.2 target size)', () => {
  for (const [size, width] of Object.entries(WIDTHS)) {
    it(`keeps the presence button at least 24 x 24 px on the ${size} (${width} px)`, () => {
      const style = computed(width, 'button', ['presence'], ['top']);
      expect(style.get('display')).toMatch(/^(inline-)?flex$/);
      expect(style.get('align-items')).toBe('center');
      // Declared floors, so neither the word's width nor the flex shrink of a crowded header can
      // take it below the minimum (on a phone the word gives way first, the button does not).
      expect(px(style.get('min-block-size') ?? ''), 'min-block-size').toBeGreaterThanOrEqual(MIN_TARGET_PX);
      expect(px(style.get('min-inline-size') ?? ''), 'min-inline-size').toBeGreaterThanOrEqual(MIN_TARGET_PX);
    });

    it(`keeps the certificate download at least 24 x 24 px on the ${size} (${width} px)`, () => {
      const style = computed(width, 'a', ['btn'], ['tiles', 'tiles--single', 'tile', 'tile-body', 'tile-actions']);
      expect(style.get('display')).toBe('inline-flex');
      expect(style.get('align-items')).toBe('center');
      expect(style.get('white-space')).toBe('nowrap'); // one line: its height never depends on wrapping
      expect(px(style.get('min-block-size') ?? ''), 'min-block-size').toBeGreaterThanOrEqual(MIN_TARGET_PX);
      // Wide enough from its padding alone, whatever the label's font does.
      expect(inlinePadding(style)).toBeGreaterThanOrEqual(MIN_TARGET_PX);
    });
  }

  it('declares the target-size floor as a token', () => {
    expect(px('var(--hit-min)')).toBe(MIN_TARGET_PX);
    expect(px('var(--hit)')).toBeGreaterThanOrEqual(MIN_TARGET_PX);
  });

  it('scrolls whatever comes into view clear of the sticky header', () => {
    // A control half under the header is both an obscured focus and a too-small target.
    const html = computed(WIDTHS.phone, 'html', []);
    expect(px(html.get('scroll-padding-top') ?? '')).toBeGreaterThanOrEqual(px('var(--top-h)')!);
  });
});
