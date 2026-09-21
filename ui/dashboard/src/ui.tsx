/**
 * The handful of shapes every page is built from: the hero (one display headline, one grey
 * subline, one or two text links), the rounded tile, the horizontal snap rail with its two round
 * chevron buttons, and the two small primitives that keep identifiers off the screen — a state word
 * and a "label on top, machine detail underneath" pair.
 *
 * Nothing here holds state about the core; these are layout primitives.
 *
 * The only icons in the dashboard are the two chevrons below, drawn inline as SVG paths: the CSP
 * forbids external assets, and an emoji arrow would not match the type.
 */

import { Children, type ReactNode, useEffect, useRef, useState } from 'react';

import type { T } from './i18n';

/* ------------------------------------------------------------------- hero - */

export interface HeroProps {
  title: string;
  sub: string;
  /** Up to two quiet text links on the right, like apple.com's "Frag unsere Specialists". */
  links?: ReactNode;
  /** The Status hero — and only that one — carries the soft gradient glow. */
  glow?: boolean;
  /** Extra content below the subline (the Settings hero shows its storage note there). */
  children?: ReactNode;
}

export function Hero({ title, sub, links, glow, children }: HeroProps) {
  return (
    <header className={glow ? 'hero hero--glow' : 'hero'}>
      <h2 className="hero-title">{title}</h2>
      <p className="hero-sub">{sub}</p>
      {links && <div className="hero-links">{links}</div>}
      {children}
    </header>
  );
}

/* ------------------------------------------------------------------- tile - */

export interface TileProps {
  /** Small uppercase label above the title. */
  eyebrow?: string;
  title: string;
  /** One sentence under the title. */
  lede?: string;
  /** Black tile instead of white (in dark mode: the bright one); one feature tile per page. */
  feature?: boolean;
  /** Spans the full tile grid. */
  wide?: boolean;
  /**
   * Heading level of the tile title. A tile inside a `<section>` that already has an H3 heading
   * passes 4, so the accessibility tree nests instead of listing the tile as a sibling of the page.
   */
  level?: 3 | 4;
  id?: string;
  className?: string;
  children?: ReactNode;
}

export function Tile({
  eyebrow,
  title,
  lede,
  feature,
  wide,
  level = 3,
  id,
  className,
  children,
}: TileProps) {
  const headingId = id ? `${id}-title` : undefined;
  const classes = ['tile'];
  if (feature) classes.push('tile--feature');
  if (wide) classes.push('tile--wide');
  if (className) classes.push(className);
  const Heading = level === 4 ? 'h4' : 'h3';
  return (
    <section id={id} className={classes.join(' ')} aria-labelledby={headingId}>
      {eyebrow && <span className="eyebrow">{eyebrow}</span>}
      <Heading id={headingId} className="tile-title">
        {title}
      </Heading>
      {lede && <p className="tile-lede">{lede}</p>}
      {children && <div className="tile-body">{children}</div>}
    </section>
  );
}

/* ------------------------------------------------------------------- rail - */

function Chevron({ back }: { back?: boolean }) {
  return (
    <svg width="15" height="15" viewBox="0 0 15 15" aria-hidden="true" focusable="false">
      <path
        d={back ? 'M9.2 2.6 4.3 7.5l4.9 4.9' : 'M5.8 2.6l4.9 4.9-4.9 4.9'}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function nudge(track: HTMLDivElement | null, direction: 1 | -1): void {
  if (!track) return;
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
  track.scrollBy({
    left: direction * Math.max(260, track.clientWidth * 0.8),
    behavior: reduce ? 'auto' : 'smooth',
  });
}

export interface RailProps {
  t: T;
  title: string;
  sub?: string;
  /** Accessible name of the scrollable region; falls back to the title. */
  label?: string;
  children: ReactNode;
}

/** A row of tiles that scrolls sideways with snap points, chevrons and no visible scrollbar. */
export function Rail({ t, title, sub, label, children }: RailProps) {
  const trackRef = useRef<HTMLDivElement | null>(null);
  const [scrollable, setScrollable] = useState(false);
  const count = Children.count(children);

  // Three tiles fill the column exactly; with nothing to scroll the chevrons say so by being off.
  useEffect(() => {
    const track = trackRef.current;
    if (!track) return;
    const update = () => setScrollable(track.scrollWidth - track.clientWidth > 4);
    update();
    if (typeof ResizeObserver !== 'function') return;
    const observer = new ResizeObserver(update);
    observer.observe(track);
    return () => observer.disconnect();
    // Re-measure when the number of tiles changes, not on every parent render.
  }, [count]);

  return (
    <section className="rail-section">
      <div className="rail-head">
        <div>
          <h3 className="rail-title">{title}</h3>
          {sub && <p className="rail-sub">{sub}</p>}
        </div>
        <div className="rail-btns">
          <button
            type="button"
            className="rail-btn"
            aria-label={t('rail_prev')}
            disabled={!scrollable}
            onClick={() => nudge(trackRef.current, -1)}
          >
            <Chevron back />
          </button>
          <button
            type="button"
            className="rail-btn"
            aria-label={t('rail_next')}
            disabled={!scrollable}
            onClick={() => nudge(trackRef.current, 1)}
          >
            <Chevron />
          </button>
        </div>
      </div>
      <div
        className="rail"
        ref={trackRef}
        role="group"
        aria-label={label ?? title}
        tabIndex={scrollable ? 0 : -1}
      >
        {children}
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ state - */

export type Tone = 'ok' | 'warn' | 'danger' | 'off';

const HEALTH_TONE: Record<string, Tone> = {
  available: 'ok',
  connected: 'ok',
  ok: 'ok',
  limited: 'warn',
  degraded: 'warn',
  denied: 'warn',
  confirm: 'warn',
  unavailable: 'danger',
  failed: 'danger',
  deny: 'danger',
};

/** The tone a health/plugin status word carries. Anything unknown reads as neutral, not as bad. */
export function toneFor(status: string): Tone {
  return HEALTH_TONE[status] ?? 'off';
}

/**
 * Status word with a small dot; the dot is a `::before`, so the cell's text stays the word alone.
 *
 * `tone` is explicit. A control that is merely *on* is not the same thing as a subsystem that is
 * *degraded*, and the two used to share the amber "limited" colour because it happened to look
 * right.
 */
export function StateWord({ tone, label }: { tone: Tone; label: string }) {
  return <span className={`state state--${tone}`}>{label}</span>;
}

/* ----------------------------------------------------------------- detail - */

/**
 * A word for people, with the machine's own string underneath in a muted line.
 *
 * This is the shape §7 asks for wherever the core sends an identifier, a path or an untranslated
 * reason: the label is what the screen says, the detail is what an operator needs in order to look
 * it up. Nothing is hidden and nothing raw is promoted to a headline.
 */
export function Detail({ label, detail }: { label: ReactNode; detail?: string | null }) {
  return (
    <span className="detail">
      <span className="detail-main">{label}</span>
      {detail ? <span className="detail-sub break">{detail}</span> : null}
    </span>
  );
}
