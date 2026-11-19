const VERSION = 'paperloom-shell-v11';
const SHELL = ['./', './index.html', './styles.css', './app.js', './store.js', './sync.js', './sync-core.js', './native.js', './navigation-core.js', './reader-state.js', './reading-core.js', './providers.js', './package-core.js', './package-worker.js', './vendor/fflate.js', './vendor/capacitor.js', './icon.svg', './manifest.webmanifest', './demo/demo.folio'];
self.addEventListener('install', event => { event.waitUntil(caches.open(VERSION).then(cache => cache.addAll(SHELL))); self.skipWaiting(); });
self.addEventListener('activate', event => { event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k.startsWith('paperloom-shell-') && k !== VERSION).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/') || event.request.headers.has('authorization')) return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok && SHELL.some(path => new URL(path, self.registration.scope).pathname === url.pathname)) {
      const copy = response.clone(); caches.open(VERSION).then(cache => cache.put(event.request, copy));
    }
    return response;
  }).catch(async () => (await caches.match(event.request)) || (event.request.mode === 'navigate' ? await caches.match('./index.html') : Response.error())));
});
