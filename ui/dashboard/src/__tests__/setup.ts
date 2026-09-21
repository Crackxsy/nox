/**
 * jsdom gaps the component tests need filled.
 *
 * jsdom implements neither `matchMedia` nor `ResizeObserver`, and the dashboard asks for both
 * (reduced motion in the rail, the rail's overflow measurement). Stubbing them here rather than in
 * the components keeps the production code free of `typeof window.matchMedia === 'function'`
 * defensive branches that only a test would ever take.
 */

import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

if (typeof window.matchMedia !== 'function') {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList;
}

if (typeof globalThis.ResizeObserver !== 'function') {
  globalThis.ResizeObserver = class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  } as unknown as typeof ResizeObserver;
}

if (typeof Element.prototype.scrollBy !== 'function') {
  Element.prototype.scrollBy = () => undefined;
}

afterEach(() => cleanup());
