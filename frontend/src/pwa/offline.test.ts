import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { HighlightMark } from "../api";
import {
  activeOfflineOwner,
  acknowledgePosition,
  applyPendingMarks,
  cacheBookForOffline,
  flushOfflineMutations,
  isBookSavedOffline,
  initializeOfflineSupport,
  pendingPosition,
  purgeOfflineTenant,
  queueMarkCreate,
  queueMarkDelete,
  queuePosition,
} from "./offline";

const book = "book";
const owner = "05dbebd1-e9e8-4be8-8172-eb65ca8d2a67";

function highlight(id = "offline-mark"): HighlightMark {
  return {
    id,
    kind: "highlight",
    anchor: { cfi: "epubcfi(/6/2!/4/2,/1:0,/1:4)", atom: 1 },
    color: "yellow",
    selected_text: "portable",
    body: null,
    label: null,
    version: 1,
    created_at: "now",
    updated_at: "now",
  };
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("litlet.offline.owner", owner);
  vi.spyOn(navigator, "onLine", "get").mockReturnValue(true);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("tenant-scoped offline outbox", () => {
  test("a delayed older fallback merges its high-water without overwriting the newer retained cursor", () => {
    queuePosition(book, `/api/books/${book}/position`, { cfi: "latest-reread", offset: 10,
      completed_chapter: 1, position_epoch: 2, base_version: 4, client_id: "reader-client", client_sequence: 9 });
    queuePosition(book, `/api/books/${book}/position`, { cfi: "older-finish", offset: 300,
      completed_chapter: 3, position_epoch: 2, base_version: 3, client_id: "reader-client", client_sequence: 8 });
    expect(pendingPosition(book)).toMatchObject({ cfi: "latest-reread", offset: 10,
      completed_chapter: 3, high_water_offset: 300, base_version: 4, client_sequence: 9 });
  });

  test("rebases a queued successor only from its accepted same-client same-epoch live write", async () => {
    const request = { cfi: "finished", offset: 300, completed_chapter: 3, position_epoch: 2,
      base_version: 8, client_id: "reader-client", client_sequence: 10 };
    queuePosition(book, `/api/books/${book}/position`, { ...request, cfi: "rereading", offset: 10, client_sequence: 11 });
    const saved = { bookmark: 3, current_chapter: 4, chapter_progress: 1,
      position_epoch: 2, position_version: 9, applied: true };
    acknowledgePosition(book, request, { ...saved, applied: false }, owner);
    acknowledgePosition(book, { ...request, client_id: "another-reader" }, saved, owner);
    acknowledgePosition(book, request, { ...saved, position_epoch: 3 }, owner);
    expect(pendingPosition(book)?.base_version).toBe(8);
    acknowledgePosition(book, request, saved, owner);
    expect(pendingPosition(book)).toMatchObject({ cfi: "rereading", offset: 10,
      completed_chapter: 3, position_epoch: 2, base_version: 9, client_sequence: 11 });
    acknowledgePosition(book, { ...request, base_version: 9, client_sequence: 12 }, { ...saved, position_version: 10 }, owner);
    expect(pendingPosition(book)).toBeNull();
  });

  test("an old-owner acknowledgment cannot mutate the newly selected owner's outbox", () => {
    const request = { cfi: "page", offset: 10, completed_chapter: 1, position_epoch: 0,
      base_version: 0, client_id: "reader-client", client_sequence: 1 };
    queuePosition(book, `/api/books/${book}/position`, request);
    localStorage.setItem("litlet.offline.owner", "local");
    queuePosition(book, `/api/books/${book}/position`, request);
    acknowledgePosition(book, request, { bookmark: 1, current_chapter: 2, chapter_progress: 0,
      position_epoch: 0, position_version: 1 }, owner);
    expect(pendingPosition(book)).toMatchObject(request);
    localStorage.setItem("litlet.offline.owner", owner);
    expect(pendingPosition(book)).toMatchObject(request);
  });

  test("coalesces progress and replays it with the current CSRF token", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-litlet-csrf=fresh-token",
    );
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          bookmark: 3,
          position_epoch: 0,
          position_version: 4,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    queuePosition(book, `/api/books/${book}/position`, {
      cfi: "first",
      offset: 4,
      completed_chapter: 1,
    });
    queuePosition(book, `/api/books/${book}/position`, {
      cfi: "latest",
      offset: 9,
      completed_chapter: 3,
    });
    expect(pendingPosition(book)).toMatchObject({
      cfi: "latest",
      completed_chapter: 3,
    });

    await flushOfflineMutations();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith(
      `/api/books/${book}/position`,
      expect.objectContaining({
        method: "PUT",
        headers: expect.objectContaining({ "X-CSRF-Token": "fresh-token" }),
        body: expect.stringContaining("latest"),
      }),
    );
    expect(pendingPosition(book)).toBeNull();
  });

  test("keeps optimistic marks visible and deleting an unsynced mark cancels its create", () => {
    const queued = queueMarkCreate(
      book,
      "highlights",
      { anchor: highlight().anchor },
      highlight(),
    );
    expect(queued.pending).toBe(true);
    expect(applyPendingMarks(book, [])).toEqual([
      expect.objectContaining({ id: "offline-mark", pending: true }),
    ]);

    queueMarkDelete(book, queued);
    expect(applyPendingMarks(book, [])).toEqual([]);
  });

  test("backward offline reading keeps the completed frontier and latest resume cursor", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=fresh-token");
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ bookmark: 3, position_epoch: 0 }), { status: 200 }),
    );
    queuePosition(book, `/api/books/${book}/position`, {
      cfi: "finished", offset: 3000, completed_chapter: 3, position_epoch: 0,
    });
    const queued = queuePosition(book, `/api/books/${book}/position`, {
      cfi: "rereading", offset: 400, completed_chapter: 0, position_epoch: 0,
    });
    expect(queued).toMatchObject({
      cfi: "rereading", current_offset: 400, bookmark: 3, high_water_offset: 3000,
    });
    expect(pendingPosition(book)).toMatchObject({
      cfi: "rereading", offset: 400, completed_chapter: 3, high_water_offset: 3000,
    });
    await flushOfflineMutations();
    const sent = JSON.parse(String(fetch.mock.calls[0][1]?.body));
    expect(sent).toMatchObject({ cfi: "rereading", offset: 400, completed_chapter: 3 });
    expect(sent).not.toHaveProperty("high_water_offset"); // hosted strict input remains unchanged
  });

  test("reset starts fresh floors and late older-pass reports cannot replace it", () => {
    queuePosition(book, `/api/books/${book}/position`, {
      cfi: "finished", offset: 3000, completed_chapter: 3, position_epoch: 0,
    });
    queuePosition(book, `/api/books/${book}/position`, {
      cfi: "new-pass", offset: 20, completed_chapter: 0, position_epoch: 1,
    });
    const delayed = queuePosition(book, `/api/books/${book}/position`, {
      cfi: "late-old-pass", offset: 3000, completed_chapter: 3, position_epoch: 0,
    });
    expect(delayed).toMatchObject({
      cfi: "new-pass", bookmark: 0, current_offset: 20, high_water_offset: 20, position_epoch: 1,
    });
    expect(pendingPosition(book)).toMatchObject({
      cfi: "new-pass", offset: 20, completed_chapter: 0, position_epoch: 1,
    });
  });

  test("position floors are isolated by owner and by book", () => {
    queuePosition(book, `/api/books/${book}/position`, {
      cfi: "owner-a", offset: 3000, completed_chapter: 3, position_epoch: 0,
    });
    queuePosition("other-book", "/api/books/other-book/position", {
      cfi: "different-book", offset: 4, completed_chapter: 0, position_epoch: 0,
    });
    expect(pendingPosition("other-book")).toMatchObject({ completed_chapter: 0, high_water_offset: 4 });
    localStorage.setItem("litlet.offline.owner", "1bd6248c-0b6d-42e4-b382-d6ae002ab6ca");
    expect(pendingPosition(book)).toBeNull();
    queuePosition(book, `/api/books/${book}/position`, {
      cfi: "owner-b", offset: 7, completed_chapter: 0, position_epoch: 0,
    });
    expect(pendingPosition(book)).toMatchObject({ cfi: "owner-b", completed_chapter: 0, high_water_offset: 7 });
    localStorage.setItem("litlet.offline.owner", owner);
    expect(pendingPosition(book)).toMatchObject({ cfi: "owner-a", completed_chapter: 3, high_water_offset: 3000 });
  });

  test("local replay sends retained high-water geometry and latest current offset together", async () => {
    localStorage.setItem("litlet.offline.owner", "local");
    vi.spyOn(document, "cookie", "get").mockReturnValue("");
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ bookmark: 3, position_epoch: 0 }), { status: 200 }),
    );
    queuePosition(book, `/api/books/${book}/position`, {
      cfi: "local-finished", offset: 3000, completed_chapter: 3, position_epoch: 0,
    });
    queuePosition(book, `/api/books/${book}/position`, {
      cfi: "local-backward", offset: 100, completed_chapter: 0, position_epoch: 0,
    });
    await flushOfflineMutations();
    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toMatchObject({
      cfi: "local-backward", offset: 100, high_water_offset: 3000, completed_chapter: 3,
    });
  });

  test("a replay response cannot delete a newer position queued during its request", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=fresh-token");
    let finishFirst!: (response: Response) => void;
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { finishFirst = resolve; }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ bookmark: 3, position_epoch: 0 }), { status: 200 }));
    queuePosition(book, `/api/books/${book}/position`, {
      cfi: "first", offset: 1000, completed_chapter: 1, position_epoch: 0,
    });
    const flushing = flushOfflineMutations();
    expect(fetch).toHaveBeenCalledTimes(1);
    queuePosition(book, `/api/books/${book}/position`, {
      cfi: "newer", offset: 3000, completed_chapter: 3, position_epoch: 0,
    });
    finishFirst(new Response(JSON.stringify({ bookmark: 1, position_epoch: 0 }), { status: 200 }));
    await flushing;
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(JSON.parse(String(fetch.mock.calls[1][1]?.body))).toMatchObject({
      cfi: "newer", offset: 3000, completed_chapter: 3,
    });
    expect(pendingPosition(book)).toBeNull();
  });

  test("a tenant switch stops an in-flight replay before it can send another owner's queue", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=fresh-token");
    let finishFirst!: (response: Response) => void;
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { finishFirst = resolve; }));
    queuePosition(book, `/api/books/${book}/position`, { cfi: "a", offset: 2 });
    queuePosition("second", "/api/books/second/position", { cfi: "a-second", offset: 3 });
    const flushing = flushOfflineMutations();
    localStorage.setItem("litlet.offline.owner", "1bd6248c-0b6d-42e4-b382-d6ae002ab6ca");
    queuePosition(book, `/api/books/${book}/position`, { cfi: "b", offset: 4 });
    finishFirst(new Response(JSON.stringify({ bookmark: 1, position_epoch: 0 }), { status: 200 }));
    await flushing;
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(pendingPosition(book)).toMatchObject({ cfi: "b", completed_chapter: 0 });
  });

  test("local mode replays queued progress without a hosted CSRF cookie", async () => {
    localStorage.setItem("litlet.offline.owner", "local");
    vi.spyOn(document, "cookie", "get").mockReturnValue("");
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          bookmark: 2,
          position_epoch: 0,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    queuePosition(book, `/api/books/${book}/position`, {
      cfi: "local",
      completed_chapter: 2,
    });

    await flushOfflineMutations();
    expect(fetch).toHaveBeenCalledWith(
      `/api/books/${book}/position`,
      expect.objectContaining({
        headers: expect.not.objectContaining({
          "X-CSRF-Token": expect.anything(),
        }),
      }),
    );
    expect(pendingPosition(book)).toBeNull();
  });

  test("explicit offline save verifies the manifest and EPUB entered the owner cache", async () => {
    const stored = new Map<string, Response>();
    vi.stubGlobal("caches", {
      open: vi.fn(async () => ({
        put: async (path: string, response: Response) => {
          stored.set(path, response);
        },
        match: async (path: string) => stored.get(path),
      })),
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (path) =>
        new Response(
          String(path).endsWith("/epub") ? new Uint8Array([1, 2, 3]) : "{}",
          {
            status: 200,
            headers: {
              "content-type": String(path).endsWith("/epub")
                ? "application/epub+zip"
                : "application/json",
            },
          },
        ),
    );

    await expect(cacheBookForOffline(book)).resolves.toBeUndefined();
    expect(stored.has(`/api/books/${book}/manifest`)).toBe(true);
    expect(stored.has(`/api/books/${book}/epub`)).toBe(true);
  });

  test("purge removes every owner cache and all reader-local state", async () => {
    localStorage.setItem("rc:reader-marks:book", "secret note");
    localStorage.setItem("litlet.reader-preferences:book", "night");
    localStorage.setItem(
      `litlet.library-recent:${owner}`,
      JSON.stringify(["private-book"]),
    );
    localStorage.setItem(
      "litlet.library-recent:local",
      JSON.stringify(["local-book"]),
    );
    queuePosition(book, `/api/books/${book}/position`, { cfi: "queued" });
    const deleted: string[] = [];
    vi.stubGlobal("caches", {
      keys: vi.fn(async () => [
        `litlet-reader-owner-${owner}`,
        "litlet-shell-v1",
      ]),
      delete: vi.fn(async (name: string) => {
        deleted.push(name);
        return true;
      }),
    });

    await purgeOfflineTenant(owner);
    expect(activeOfflineOwner()).toBe("local");
    expect(localStorage.getItem("rc:reader-marks:book")).toBeNull();
    expect(localStorage.getItem("litlet.reader-preferences:book")).toBeNull();
    expect(localStorage.getItem(`litlet.library-recent:${owner}`)).toBeNull();
    expect(localStorage.getItem("litlet.library-recent:local")).toBe(
      JSON.stringify(["local-book"]),
    );
    expect(pendingPosition(book)).toBeNull();
    expect(deleted).toEqual([`litlet-reader-owner-${owner}`]);
  });

  test("reports offline availability only when both required files exist in this owner's cache", async () => {
    const paths = new Map<string, Response>();
    const match = vi.fn(async (path: string, options: { cacheName: string }) =>
      options.cacheName === `litlet-reader-owner-${owner}`
        ? paths.get(path)
        : undefined,
    );
    const open = vi.fn();
    vi.stubGlobal("caches", { match, open });
    paths.set(`/api/books/${book}/epub`, new Response("epub"));
    expect(await isBookSavedOffline(book)).toBe(false);
    paths.set(`/api/books/${book}/manifest`, new Response("{}"));
    expect(await isBookSavedOffline(book)).toBe(true);
    expect(await isBookSavedOffline(book, "local")).toBe(false);
    expect(open).not.toHaveBeenCalled();
    expect(match).toHaveBeenCalledWith(`/api/books/${book}/manifest`, {
      cacheName: `litlet-reader-owner-${owner}`,
    });
  });

  test("a stalled session check aborts and resumes the same owner's valid offline session", async () => {
    vi.useFakeTimers();
    vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
    const saved = {
      user: { id: owner, display_name: "Offline reader" },
      expires_at: new Date(Date.now() + 60_000).toISOString(),
    };
    localStorage.setItem("litlet.offline.session", JSON.stringify(saved));
    let signal: AbortSignal | undefined;
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (_, options) =>
        new Promise((_, reject) => {
          signal = options?.signal ?? undefined;
          signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );
    const initialized = initializeOfflineSupport();
    await vi.advanceTimersByTimeAsync(8_000);
    expect(signal?.aborted).toBe(true);
    expect(await initialized).toEqual({ ...saved, offline: true });
    expect(activeOfflineOwner()).toBe(owner);
  });
});
