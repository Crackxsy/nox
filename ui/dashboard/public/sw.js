/* global self, caches, fetch, URL -- a service worker, not a page */
/*
 * Nox dashboard service worker: makes the dashboard installable on a phone's home screen and lets
 * it open its own shell when the network is briefly away.
 *
 * It keeps this build's files only - the page, its scripts and styles, the icons. Nothing the core
 * says is ever stored: there is no HTTP API to cache, and the WebSocket does not pass through here.
 * Pages are network-first, so a new version of Nox is picked up on the next visit; hashed assets
 * are cache-first, because a hashed file never changes.
 */

const CACHE = 'nox-shell-v1';
const SHELL = ['./', './manifest.webmanifest', './icon-192.png', './icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith('/dashboard/')) return;

  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match('./')));
    return;
  }
  if (url.pathname.startsWith('/dashboard/assets/')) {
    event.respondWith(
      caches.match(request).then(
        (hit) =>
          hit ||
          fetch(request).then((response) => {
            if (response.ok) {
              const copy = response.clone();
              void caches.open(CACHE).then((cache) => cache.put(request, copy));
            }
            return response;
          }),
      ),
    );
  }
});
