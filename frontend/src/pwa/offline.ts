/// <reference types="vite/client" />
import type { Position, PutPosition, ReaderMark } from "../api";

const ACTIVE_OWNER_KEY = "litlet.offline.owner";
const SESSION_KEY = "litlet.offline.session";
const OUTBOX_PREFIX = "litlet.offline.outbox:";
const CACHE_PREFIX = "litlet-reader-owner-";
const LOCAL_OWNER = "local";
const MAX_OUTBOX_MUTATIONS = 1000;
const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export type OfflineUser = { id: string; display_name: string; email?: string };
export type OfflineSession = {
  user: OfflineUser;
  expires_at: string;
  offline?: boolean;
};

type PositionMutation = {
  id: string;
  kind: "position";
  owner: string;
  bookId: string;
  method: "PUT";
  url: string;
  body: Record<string, unknown>;
  highWaterOffset?: number;
  createdAt: number;
};

type MarkMutation = {
  id: string;
  kind: "mark-create" | "mark-update" | "mark-delete";
  owner: string;
  bookId: string;
  method: "POST" | "PATCH" | "DELETE";
  url: string;
  body?: Record<string, unknown>;
  optimisticMark: ReaderMark | null;
  createdAt: number;
};

type OfflineMutation = PositionMutation | MarkMutation;

let flushPromise: Promise<void> | null = null;
let listenersInstalled = false;
const positionDeliveries = new Map<string, Set<Promise<unknown>>>();
const positionAcknowledgments = new Map<string, { deliveryKey: string; epoch: number; sequence: number; version: number }>();

/** Offline replay must follow any earlier live write for the same owner and book. */
export function trackPositionDelivery<T>(bookId: string, owner: string, send: () => Promise<T>): Promise<T> {
  const key = `${owner}:${bookId}`;
  const active = positionDeliveries.get(key) ?? new Set<Promise<unknown>>();
  const predecessor = active.size ? Promise.allSettled([...active]) : null;
  positionDeliveries.set(key, active);
  const delivery = predecessor ? predecessor.then(send) : send();
  const tracked = delivery.finally(() => {
    active.delete(tracked);
    if (!active.size) {
      positionDeliveries.delete(key);
      for (const [clockKey, clock] of positionAcknowledgments)
        if (clock.deliveryKey === key) positionAcknowledgments.delete(clockKey);
    }
  });
  active.add(tracked);
  return tracked;
}

async function waitForPositionDeliveries(bookId: string, owner: string): Promise<void> {
  const key = `${owner}:${bookId}`;
  while (positionDeliveries.get(key)?.size)
    await Promise.allSettled([...positionDeliveries.get(key)!]);
}

function storage(): Storage | null {
  try {
    return globalThis.localStorage;
  } catch {
    return null;
  }
}

function validOwner(value: string | null): value is string {
  return value === LOCAL_OWNER || (value != null && UUID.test(value));
}

export function activeOfflineOwner(): string {
  const value = storage()?.getItem(ACTIVE_OWNER_KEY) ?? null;
  return validOwner(value) ? value : LOCAL_OWNER;
}

export function offlineOwnerCacheName(owner = activeOfflineOwner()): string {
  return `${CACHE_PREFIX}${owner}`;
}

/** Check downloaded reader files without creating a cache or fetching any book content. */
export async function isBookSavedOffline(
  bookId: string,
  owner = activeOfflineOwner(),
): Promise<boolean> {
  if (!("caches" in globalThis)) return false;
  try {
    const options = { cacheName: offlineOwnerCacheName(owner) };
    const [manifest, epub] = await Promise.all([
      caches.match(`/api/books/${bookId}/manifest`, options),
      caches.match(`/api/books/${bookId}/epub`, options),
    ]);
    return Boolean(manifest?.ok && epub?.ok);
  } catch {
    return false;
  }
}

function outboxKey(owner = activeOfflineOwner()): string {
  return `${OUTBOX_PREFIX}${owner}`;
}

function readOutbox(owner = activeOfflineOwner()): OfflineMutation[] {
  try {
    const value = JSON.parse(
      storage()?.getItem(outboxKey(owner)) ?? "[]",
    ) as unknown;
    if (!Array.isArray(value)) return [];
    return value.filter(
      (item): item is OfflineMutation =>
        item != null &&
        typeof item === "object" &&
        (item as OfflineMutation).owner === owner,
    );
  } catch {
    return [];
  }
}

function writeOutbox(
  items: OfflineMutation[],
  owner = activeOfflineOwner(),
): void {
  const target = storage();
  if (!target) return;
  if (items.length) target.setItem(outboxKey(owner), JSON.stringify(items));
  else target.removeItem(outboxKey(owner));
}

function upsert(item: OfflineMutation): void {
  const items = readOutbox(item.owner).filter(
    (candidate) => candidate.id !== item.id,
  );
  if (items.length >= MAX_OUTBOX_MUTATIONS)
    throw new Error("the offline sync queue is full");
  items.push(item);
  writeOutbox(items, item.owner);
}

function csrfToken(): string | null {
  const entry = document.cookie
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith("__Host-litlet-csrf="));
  if (!entry) return null;
  const value = entry.slice(entry.indexOf("=") + 1);
  return value ? decodeURIComponent(value) : null;
}

function postToWorker(message: Record<string, unknown>): void {
  if (import.meta.env.DEV) return;
  if (!("serviceWorker" in navigator)) return;
  const worker = navigator.serviceWorker.controller;
  if (worker) worker.postMessage(message);
  else
    void navigator.serviceWorker.ready
      .then((registration) => {
        registration.active?.postMessage(message);
      })
      .catch(() => {});
}

function dispatchSync(detail: Record<string, unknown>): void {
  globalThis.dispatchEvent(new CustomEvent("litlet:offline-sync", { detail }));
}

async function deleteOwnerCaches(owner: string): Promise<void> {
  if (!("caches" in globalThis)) return;
  const names = await caches.keys();
  await Promise.all(
    names
      .filter((name) => name === offlineOwnerCacheName(owner))
      .map((name) => caches.delete(name)),
  );
}

function clearReaderStorage(): void {
  const target = storage();
  if (!target) return;
  const prefixes = [
    "rc:reader-marks:",
    "rc:lastSeen:",
    "litlet.reader-preferences:",
    "litlet.position.",
  ];
  const keys = Array.from({ length: target.length }, (_, index) =>
    target.key(index),
  ).filter((key): key is string => key != null);
  for (const key of keys)
    if (prefixes.some((prefix) => key.startsWith(prefix)))
      target.removeItem(key);
}

export async function purgeOfflineTenant(
  owner = activeOfflineOwner(),
): Promise<void> {
  const target = storage();
  target?.removeItem(outboxKey(owner));
  target?.removeItem(`litlet.library-recent:${owner}`);
  target?.removeItem(SESSION_KEY);
  clearReaderStorage();
  postToWorker({ type: "PURGE_OWNER", owner });
  await deleteOwnerCaches(owner);
  if (target?.getItem(ACTIVE_OWNER_KEY) === owner)
    target.removeItem(ACTIVE_OWNER_KEY);
}

async function setOwner(owner: string): Promise<void> {
  const normalized = validOwner(owner) ? owner : LOCAL_OWNER;
  const previous = activeOfflineOwner();
  if (previous !== normalized) await purgeOfflineTenant(previous);
  storage()?.setItem(ACTIVE_OWNER_KEY, normalized);
  postToWorker({ type: "SET_OWNER", owner: normalized });
}

export async function initializeOfflineSupport(): Promise<
  OfflineSession | null | undefined
> {
  if (import.meta.env.PROD && "serviceWorker" in navigator) {
    await navigator.serviceWorker
      .register("/sw.js", { scope: "/" })
      .catch(() => undefined);
  }
  let session: OfflineSession | null | undefined;
  const sessionController = new AbortController();
  const sessionTimeout = globalThis.setTimeout(
    () => sessionController.abort(),
    8_000,
  );
  try {
    const response = await fetch("/api/auth/session", {
      cache: "no-store",
      signal: sessionController.signal,
    });
    if (response.ok) {
      session = (await response.json()) as OfflineSession;
      if (!validOwner(session.user.id) || session.user.id === LOCAL_OWNER) {
        await purgeOfflineTenant();
        return null;
      }
      await setOwner(session.user.id);
      storage()?.setItem(
        SESSION_KEY,
        JSON.stringify({
          user: {
            id: session.user.id,
            display_name: session.user.display_name,
          },
          expires_at: session.expires_at,
        }),
      );
    } else if (response.status === 404) {
      await setOwner(LOCAL_OWNER);
      session = undefined;
    } else if (response.status === 401) {
      await purgeOfflineTenant();
      session = null;
    } else {
      throw new TypeError("the session service is unavailable");
    }
  } catch {
    const saved = storage()?.getItem(SESSION_KEY);
    try {
      session = saved
        ? { ...(JSON.parse(saved) as OfflineSession), offline: true }
        : undefined;
      const expiry = session ? Date.parse(session.expires_at) : Number.NaN;
      if (session && (!Number.isFinite(expiry) || expiry <= Date.now())) {
        await purgeOfflineTenant();
        session = null;
      }
    } catch {
      session = undefined;
    }
    postToWorker({ type: "SET_OWNER", owner: activeOfflineOwner() });
  } finally {
    globalThis.clearTimeout(sessionTimeout);
  }
  if (!listenersInstalled) {
    globalThis.addEventListener("online", () => {
      void flushOfflineMutations();
    });
    listenersInstalled = true;
  }
  if (navigator.onLine) void flushOfflineMutations();
  return session;
}

export async function cacheBookForOffline(bookId: string): Promise<void> {
  const paths = [
    `/api/books/${bookId}/manifest`,
    `/api/books/${bookId}/epub`,
    `/api/books/${bookId}/position`,
    `/api/books/${bookId}/marks`,
    `/api/books/${bookId}/preferences`,
  ];
  const results = await Promise.allSettled(paths.map((path) => fetch(path)));
  const manifest = results[0].status === "fulfilled" ? results[0].value : null;
  const epub = results[1].status === "fulfilled" ? results[1].value : null;
  if (!manifest?.ok || !epub?.ok || !("caches" in globalThis)) {
    throw new Error("the book could not be saved for offline reading");
  }
  const cache = await caches.open(offlineOwnerCacheName());
  try {
    await Promise.all(
      results.map(async (result, index) => {
        if (result.status === "fulfilled" && result.value.ok) {
          await cache.put(paths[index], result.value.clone());
        }
      }),
    );
  } catch {
    throw new Error(
      "the browser did not have enough storage to save this book offline",
    );
  }
  if (!(await cache.match(paths[0])) || !(await cache.match(paths[1]))) {
    throw new Error(
      "the browser did not have enough storage to save this book offline",
    );
  }
}

export function queuePosition(
  bookId: string,
  url: string,
  body: Record<string, unknown>,
): Position {
  const owner = activeOfflineOwner();
  const previous = readOutbox(owner).find(
    (candidate): candidate is PositionMutation =>
      candidate.kind === "position" && candidate.bookId === bookId,
  );
  const epoch = positionOrdinal(body.position_epoch);
  // A delayed report from a pre-reset tab cannot erase the newer pass's queued position.
  if (previous && positionOrdinal(previous.body.position_epoch) > epoch)
    return queuedPosition(previous);
  const samePass = previous && positionOrdinal(previous.body.position_epoch) === epoch;
  const latest = samePass && typeof body.client_id === "string"
    && body.client_id === previous.body.client_id
    && positionOrdinal(body.client_sequence) < positionOrdinal(previous.body.client_sequence)
    ? previous.body : body;
  const completed = Math.max(
    positionOrdinal(body.completed_chapter),
    samePass ? positionOrdinal(previous.body.completed_chapter) : 0,
  );
  const highWaterOffset = Math.max(
    positionOrdinal(body.offset),
    positionOrdinal(body.high_water_offset),
    samePass ? queuedHighWater(previous) : 0,
  );
  const mutation: PositionMutation = {
    id: `position:${bookId}`,
    kind: "position",
    owner,
    bookId,
    method: "PUT",
    url,
    body: {
      ...latest,
      completed_chapter: completed,
      // The local API derives completed chapters from geometry. Hosted mode already accepts the
      // explicit completed floor and rejects unknown request fields, so keep this local-only.
      ...(owner === LOCAL_OWNER ? { high_water_offset: highWaterOffset } : {}),
    },
    highWaterOffset,
    createdAt: Date.now(),
  };
  upsert(mutation);
  return queuedPosition(mutation);
}

function positionOrdinal(value: unknown): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

function queuedHighWater(item: PositionMutation): number {
  return Math.max(
    positionOrdinal(item.highWaterOffset),
    positionOrdinal(item.body.high_water_offset),
    positionOrdinal(item.body.offset),
  );
}

function queuedPosition(item: PositionMutation): Position {
  const body = item.body;
  const completed = positionOrdinal(body.completed_chapter);
  const offset = positionOrdinal(body.offset);
  return {
    bookmark: completed,
    completed_chapter: completed,
    current_chapter: completed + 1,
    current_offset: offset,
    high_water_offset: queuedHighWater(item),
    cfi: typeof body.cfi === "string" ? body.cfi : null,
    position_epoch: positionOrdinal(body.position_epoch),
    position_version: positionOrdinal(body.base_version),
    queued: true,
  } as Position;
}

export function pendingPosition(
  bookId: string,
): Record<string, unknown> | null {
  const item = readOutbox().find(
    (candidate): candidate is PositionMutation =>
      candidate.kind === "position" && candidate.bookId === bookId,
  );
  return item ? { ...item.body, high_water_offset: queuedHighWater(item) } : null;
}

/** Rebase only a successor of this accepted request, never unrelated remote reading progress. */
export function acknowledgePosition(
  bookId: string,
  request: Record<string, unknown>,
  saved: PutPosition,
  owner: string,
): void {
  if (activeOfflineOwner() !== owner || saved.applied === false) return;
  if (positionDeliveries.has(`${owner}:${bookId}`) && typeof request.client_id === "string"
    && positionOrdinal(request.position_epoch) === saved.position_epoch
    && positionOrdinal(saved.position_version) > positionOrdinal(request.base_version)) {
    const key = `${owner}:${bookId}:${request.client_id}`;
    const previous = positionAcknowledgments.get(key);
    const epoch = saved.position_epoch;
    const sequence = positionOrdinal(request.client_sequence);
    if (!previous || epoch > previous.epoch || (epoch === previous.epoch && sequence >= previous.sequence))
      positionAcknowledgments.set(key, { deliveryKey: `${owner}:${bookId}`, epoch, sequence, version: positionOrdinal(saved.position_version) });
  }
  const item = readOutbox(owner).find(
    (candidate): candidate is PositionMutation => candidate.kind === "position" && candidate.bookId === bookId,
  );
  if (!item || typeof request.client_id !== "string" || request.client_id !== item.body.client_id
    || positionOrdinal(request.position_epoch) !== saved.position_epoch
    || positionOrdinal(item.body.position_epoch) !== saved.position_epoch) return;
  if (positionOrdinal(request.client_sequence) >= positionOrdinal(item.body.client_sequence)
    && saved.bookmark >= positionOrdinal(item.body.completed_chapter)) {
    removeMutation(item);
  } else if (positionOrdinal(item.body.base_version) === positionOrdinal(request.base_version)
    && positionOrdinal(saved.position_version) > positionOrdinal(request.base_version)) {
    upsert({ ...item, body: { ...item.body, base_version: saved.position_version } });
  }
}

/** Only a later same-client intent may inherit a causally preceding successful write's clock. */
export function positionRequestBody(bookId: string, owner: string, body: Record<string, unknown>): Record<string, unknown> {
  if (activeOfflineOwner() !== owner || typeof body.client_id !== "string") return body;
  const acknowledged = positionAcknowledgments.get(`${owner}:${bookId}:${body.client_id}`);
  return acknowledged && positionOrdinal(body.position_epoch) === acknowledged.epoch
    && positionOrdinal(body.client_sequence) > acknowledged.sequence
    && positionOrdinal(body.base_version) < acknowledged.version
    ? { ...body, base_version: acknowledged.version } : body;
}

/** A successful explicit reset discards only older-pass intent for this owner and book. */
export function discardPositionBeforeEpoch(bookId: string, epoch: number, owner: string): void {
  if (activeOfflineOwner() !== owner) return;
  const items = readOutbox(owner).filter((item) => item.kind !== "position" || item.bookId !== bookId
    || positionOrdinal(item.body.position_epoch) >= epoch);
  writeOutbox(items, owner);
}

export function queueMarkCreate(
  bookId: string,
  collection: string,
  body: Record<string, unknown>,
  optimisticMark: ReaderMark,
): ReaderMark {
  const owner = activeOfflineOwner();
  const mark = { ...optimisticMark, pending: true } as ReaderMark;
  upsert({
    id: `mark:create:${mark.id}`,
    kind: "mark-create",
    owner,
    bookId,
    method: "POST",
    url: `/api/books/${bookId}/${collection}`,
    body,
    optimisticMark: mark,
    createdAt: Date.now(),
  });
  return mark;
}

export function queueMarkUpdate(
  bookId: string,
  mark: ReaderMark,
  body: Record<string, unknown>,
): ReaderMark {
  const owner = activeOfflineOwner();
  const optimisticMark = {
    ...mark,
    ...body,
    pending: true,
    updated_at: new Date().toISOString(),
  } as ReaderMark;
  const items = readOutbox(owner);
  const create = items.find(
    (item) =>
      item.kind === "mark-create" && item.optimisticMark?.id === mark.id,
  );
  if (create?.kind === "mark-create") {
    create.body = { ...create.body, ...body };
    create.optimisticMark = optimisticMark;
    writeOutbox(items, owner);
    return optimisticMark;
  }
  upsert({
    id: `mark:update:${mark.kind}:${mark.id}`,
    kind: "mark-update",
    owner,
    bookId,
    method: "PATCH",
    url: `/api/books/${bookId}/${mark.kind}s/${mark.id}`,
    body,
    optimisticMark,
    createdAt: Date.now(),
  });
  return optimisticMark;
}

export function queueMarkDelete(bookId: string, mark: ReaderMark): void {
  const owner = activeOfflineOwner();
  const items = readOutbox(owner);
  const withoutCreate = items.filter(
    (item) =>
      !(item.kind === "mark-create" && item.optimisticMark?.id === mark.id),
  );
  if (withoutCreate.length !== items.length) {
    writeOutbox(withoutCreate, owner);
    return;
  }
  writeOutbox(
    withoutCreate.filter(
      (item) =>
        !(item.kind === "mark-update" && item.optimisticMark?.id === mark.id),
    ),
    owner,
  );
  upsert({
    id: `mark:delete:${mark.kind}:${mark.id}`,
    kind: "mark-delete",
    owner,
    bookId,
    method: "DELETE",
    url: `/api/books/${bookId}/${mark.kind}s/${mark.id}`,
    optimisticMark: null,
    createdAt: Date.now(),
  });
}

export function applyPendingMarks(
  bookId: string,
  serverMarks: ReaderMark[],
): ReaderMark[] {
  let result = [...serverMarks];
  const items = readOutbox()
    .filter(
      (item): item is MarkMutation =>
        item.kind !== "position" && item.bookId === bookId,
    )
    .sort((left, right) => left.createdAt - right.createdAt);
  for (const item of items) {
    if (item.kind === "mark-delete") {
      const id = item.id.split(":").at(-1);
      result = result.filter((mark) => mark.id !== id);
    } else if (item.optimisticMark) {
      result = result.filter((mark) => mark.id !== item.optimisticMark?.id);
      result.push(item.optimisticMark);
    }
  }
  return result;
}

function removeMutation(item: OfflineMutation): boolean {
  const items = readOutbox(item.owner);
  const current = items.find((candidate) => candidate.id === item.id);
  // A new relocation can replace this stable id while the request is awaiting its response.
  if (current && JSON.stringify(current) !== JSON.stringify(item)) return false;
  writeOutbox(
    items.filter((candidate) => candidate.id !== item.id),
    item.owner,
  );
  return true;
}

export async function flushOfflineMutations(): Promise<void> {
  if (flushPromise) return flushPromise;
  flushPromise = (async () => {
    if (!navigator.onLine) return;
    const owner = activeOfflineOwner();
    const token = csrfToken();
    if (!token && owner !== LOCAL_OWNER) return;
    const items = readOutbox(owner).sort((left, right) => {
      if (left.kind === "position" && right.kind !== "position") return -1;
      if (left.kind !== "position" && right.kind === "position") return 1;
      return left.createdAt - right.createdAt;
    });
    for (let index = 0; index < items.length && index < MAX_OUTBOX_MUTATIONS; index++) {
      if (activeOfflineOwner() !== owner) break;
      let item = readOutbox(owner).find((candidate) => candidate.id === items[index].id);
      if (!item) continue;
      if (item.kind === "position" && positionDeliveries.has(`${owner}:${item.bookId}`)) {
        await waitForPositionDeliveries(item.bookId, owner);
        if (activeOfflineOwner() !== owner) break;
        item = readOutbox(owner).find((candidate) => candidate.id === items[index].id);
        if (!item) continue;
      }
      const removeOrScheduleSuccessor = () => {
        if (!removeMutation(item)) {
          const successor = readOutbox(owner).find((candidate) => candidate.id === item.id);
          if (successor) items.splice(index + 1, 0, successor);
        }
      };
      try {
        const deliver = async () => {
          const body = item.kind === "position" ? positionRequestBody(item.bookId, owner, item.body) : item.body;
          const response = await fetch(item.url, {
            method: item.method,
            headers: {
              ...(token ? { "X-CSRF-Token": token } : {}),
              ...(body ? { "content-type": "application/json" } : {}),
            },
            ...(body ? { body: JSON.stringify(body) } : {}),
            keepalive: true,
          });
          const value = response.ok && response.status !== 204
            ? await response.json().catch(() => null) : null;
          if (response.ok && item.kind === "position" && value && typeof value === "object")
            acknowledgePosition(item.bookId, body!, value as PutPosition, owner);
          return { response, value };
        };
        const { response, value } = item.kind === "position"
          ? await trackPositionDelivery(item.bookId, owner, deliver) : await deliver();
        if (activeOfflineOwner() !== owner) break;
        if (response.ok) {
          removeOrScheduleSuccessor();
          dispatchSync({
            kind: item.kind,
            bookId: item.bookId,
            optimisticMark:
              item.kind === "position" ? null : item.optimisticMark,
            value,
          });
          continue;
        }
        if ([400, 404, 409, 410, 422].includes(response.status)) {
          removeOrScheduleSuccessor();
          dispatchSync({
            kind: item.kind,
            bookId: item.bookId,
            failed: true,
            status: response.status,
          });
          continue;
        }
        break;
      } catch {
        break;
      }
    }
  })().finally(() => {
    flushPromise = null;
  });
  return flushPromise;
}
