/**
 * jsdom gaps the component tests need filled.
 *
 * jsdom implements neither `matchMedia` nor `ResizeObserver`, and the dashboard asks for both
 * (reduced motion in the rail, the rail's overflow measurement). Stubbing them here rather than in
 * the components keeps the production code free of `typeof window.matchMedia === 'function'`
 * defensive branches that only a test would ever take.
 */

import { afterEach, beforeEach } from 'vitest';
import { cleanup } from '@testing-library/react';

import { loadLang, loadMore } from '../i18n';

// The app loads English and the area copy on demand; tests use all of it synchronously.
await loadLang('en');
await loadLang('de');
await loadMore('de');
await loadMore('en');

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

/**
 * jsdom has `<dialog>` but not `showModal()` / `close()`. The polyfill keeps what the shell relies
 * on: `open` follows the calls, Escape on an open modal dialog raises `cancel` (and closes it unless
 * the handler prevents that), and `close()` raises `close`.
 */
if (typeof HTMLDialogElement !== 'undefined' && typeof HTMLDialogElement.prototype.showModal !== 'function') {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.setAttribute('open', '');
    this.dataset.modal = 'true';
  };
  HTMLDialogElement.prototype.show = function show(this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    if (!this.hasAttribute('open')) return;
    this.removeAttribute('open');
    delete this.dataset.modal;
    this.dispatchEvent(new Event('close'));
  };
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || e.defaultPrevented) return;
    const open = [...document.querySelectorAll<HTMLDialogElement>('dialog[open][data-modal]')];
    const top = open[open.length - 1];
    if (!top) return;
    const cancel = new Event('cancel', { cancelable: true });
    if (top.dispatchEvent(cancel)) top.close();
  });
}

// The first-run flow covers the page until a name is set; tests of the shell start past it (the
// flow's own test clears this).
beforeEach(() => {
  window.localStorage.setItem('nox.firstRun', 'done');
});

afterEach(() => {
  cleanup();
  // The shell keeps its route in the fragment; every test starts on Heute again.
  window.history.replaceState(null, '', window.location.pathname);
});
