/**
 * The handful of shapes every page is built from: the hero (one display headline, one grey
 * subline, one or two text links), the rounded tile, and the horizontal snap rail with its two
 * round chevron buttons. Nothing here holds state about the core — these are layout primitives.
 *
 * The only icons in the dashboard are the two chevrons below, drawn inline as SVG paths: the CSP
 * forbids external assets, and an emoji arrow would not match the type.
 */

import { Children, type ReactNode, useCallback, useEffect, useRef, useState } from 'react';

import type { T } from './i18n';

/* ------------------------------------------------------------------- hero - */

export interface HeroProps {
  title: string;
  sub: string;
  /** Up to two quiet text links on the right, like apple.com's "Frag unsere Specialists". */
  links?: ReactNode;
  /** The Status hero — and only that one — carries the soft gradient glow. */
  glow?: boolean;
  /** Extra content below the subline (the Status hero shows its "as of" line there). */
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
  /** Black tile instead of white; used for the one feature tile per page. */
  feature?: boolean;
  /** Spans the full tile grid. */
  wide?: boolean;
  id?: string;
  className?: string;
  children?: ReactNode;
}

export function Tile({ eyebrow, title, lede, feature, wide, id, className, children }: TileProps) {
  const headingId = id ? `${id}-title` : undefined;
  const classes = ['tile'];
  if (feature) classes.push('tile--feature');
  if (wide) classes.push('tile--wide');
  if (className) classes.push(className);
  return (
    <section id={id} className={classes.join(' ')} aria-labelledby={headingId}>
      {eyebrow && <span className="eyebrow">{eyebrow}</span>}
      <h3 id={headingId} className="tile-title">
        {title}
      </h3>
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

  // Three tiles fill the column exactly; with nothing to scroll the chevrons say so by being off.
  useEffect(() => {
    const track = trackRef.current;
    if (!track) return;
    const update = () => setScrollable(track.scrollWidth - track.clientWidth > 4);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(track);
    return () => observer.disconnect();
    // Re-measure when the number of tiles changes, not on every parent render.
  }, [Children.count(children)]);

  const nudge = useCallback((direction: 1 | -1) => {
    const track = trackRef.current;
    if (!track) return;
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    track.scrollBy({
      left: direction * Math.max(260, track.clientWidth * 0.8),
      behavior: reduce ? 'auto' : 'smooth',
    });
  }, []);

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
            onClick={() => nudge(-1)}
          >
            <Chevron back />
          </button>
          <button
            type="button"
            className="rail-btn"
            aria-label={t('rail_next')}
            disabled={!scrollable}
            onClick={() => nudge(1)}
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

const STATE_TONE: Record<string, string> = {
  available: 'state--ok',
  connected: 'state--ok',
  ok: 'state--ok',
  limited: 'state--warn',
  degraded: 'state--warn',
  denied: 'state--warn',
  unavailable: 'state--danger',
  failed: 'state--danger',
};

/** Status word with a small dot; the dot is a `::before`, so the cell's text stays the word alone. */
export function StateWord({ status, label }: { status: string; label: string }) {
  return <span className={`state ${STATE_TONE[status] ?? 'state--off'}`}>{label}</span>;
}
