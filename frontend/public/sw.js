const SHELL_CACHE = "litlet-shell-v1";
const OWNER_CACHE_PREFIX = "litlet-reader-owner-";
const SHELL_ASSETS = ["/", "/index.html", "/manifest.webmanifest", "/litlet-icon.svg"];
let activeOwner = null;
const OWNER = /^(local|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$/i;

function ownerCache() {
  return activeOwner ? `${OWNER_CACHE_PREFIX}${activeOwner}` : null;
}

function isOfflineBookRead(url) {
  if (!url.pathname.startsWith("/api/books")) return false;
  return url.pathname === "/api/books" || /^\/api\/books\/[^/]+\/(manifest|epub|position|marks|preferences)$/.test(url.pathname);
}

async function cacheSafe(cache, request, response) {
  if (response.ok && response.type !== "opaque") {
    await cache.put(request, response.clone()).catch(() => false);
  }
  return response;
}

async function ownerNetworkFirst(request) {
  const name = ownerCache();
  if (!name) return fetch(request);
  const cache = await caches.open(name);
  try {
    return await cacheSafe(cache, request, await fetch(request));
  } catch (error) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw error;
  }
}

async function shellResponse(request) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  return cacheSafe(cache, request, await fetch(request));
}

async function navigationResponse(request) {
  const cache = await caches.open(SHELL_CACHE);
  try {
    const response = await fetch(request);
    if (response.ok && response.headers.get("content-type")?.includes("text/html")) {
      await cache.put("/", response.clone()).catch(() => false);
    }
    return response;
  } catch (error) {
    const cached = await cache.match("/") || await cache.match("/index.html");
    if (cached) return cached;
    throw error;
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    await Promise.allSettled(SHELL_ASSETS.map(async (path) => {
      const response = await fetch(path, { cache: "reload" });
      if (response.ok) await cache.put(path, response);
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((name) => (
      name.startsWith("litlet-shell-") && name !== SHELL_CACHE
    )).map((name) => caches.delete(name)));
    await self.clients.claim();
  })());
});

self.addEventListener("message", (event) => {
  const message = event.data;
  if (!message || typeof message !== "object") return;
  if (message.type === "SET_OWNER" && typeof message.owner === "string" && OWNER.test(message.owner)) {
    activeOwner = message.owner;
  }
  if (message.type === "PURGE_OWNER" && typeof message.owner === "string" && OWNER.test(message.owner)) {
    event.waitUntil(caches.delete(`${OWNER_CACHE_PREFIX}${message.owner}`));
    if (activeOwner === message.owner) activeOwner = null;
  }
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/auth/")) return;
  if (isOfflineBookRead(url)) {
    event.respondWith(ownerNetworkFirst(request));
    return;
  }
  if (request.mode === "navigate") {
    event.respondWith(navigationResponse(request));
    return;
  }
  if (!url.pathname.startsWith("/api/")) event.respondWith(shellResponse(request));
});
