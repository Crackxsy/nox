/**
 * Around every lazily loaded part (areas, palette, menus, the permission sheet, the first run).
 *
 * A chunk that fails to load - a stale file name after Nox was updated, a phone losing the Wi-Fi
 * for a moment - would otherwise unmount the whole page, Stopp included. Here it becomes one line
 * that says so and offers to reload (a rejected `import()` is cached by `React.lazy`, so reloading
 * the page is the retry that actually works).
 */

import { Component, type ReactNode, useEffect } from 'react';

import type { T } from '../i18n';

interface ChunkBoundaryProps {
  t: T;
  children: ReactNode;
  /** Shown above the page (palette, sheet, first run) instead of in its place. */
  floating?: boolean;
}

export class ChunkBoundary extends Component<ChunkBoundaryProps, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  componentDidCatch(error: unknown): void {
    console.error('a part of the dashboard did not load', error);
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    const { t, floating } = this.props;
    return (
      <div role="alert" className={floating ? 'alert chunk-failed chunk-failed--floating' : 'alert chunk-failed'}>
        {t('ux_chunk_failed')}{' '}
        <button type="button" className="btn btn--plain" onClick={() => window.location.reload()}>
          {t('ux_reload')}
        </button>
      </div>
    );
  }
}

/** Calls `onMount` once the boundary it sits in has really rendered (its lazy siblings loaded). */
export function Mounted({ onMount }: { onMount: () => void }) {
  useEffect(() => {
    onMount();
  }, [onMount]);
  return null;
}
