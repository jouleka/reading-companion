import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { HighlightMark } from "../api";
import {
  activeOfflineOwner,
  applyPendingMarks,
  cacheBookForOffline,
  flushOfflineMutations,
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
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("tenant-scoped offline outbox", () => {
  test("coalesces progress and replays it with the current CSRF token", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=fresh-token");
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      bookmark: 3, position_epoch: 0, position_version: 4,
    }), { status: 200, headers: { "content-type": "application/json" } }));
    queuePosition(book, `/api/books/${book}/position`, { cfi: "first", offset: 4, completed_chapter: 1 });
    queuePosition(book, `/api/books/${book}/position`, { cfi: "latest", offset: 9, completed_chapter: 3 });
    expect(pendingPosition(book)).toMatchObject({ cfi: "latest", completed_chapter: 3 });

    await flushOfflineMutations();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith(`/api/books/${book}/position`, expect.objectContaining({
      method: "PUT",
      headers: expect.objectContaining({ "X-CSRF-Token": "fresh-token" }),
      body: expect.stringContaining("latest"),
    }));
    expect(pendingPosition(book)).toBeNull();
  });

  test("keeps optimistic marks visible and deleting an unsynced mark cancels its create", () => {
    const queued = queueMarkCreate(book, "highlights", { anchor: highlight().anchor }, highlight());
    expect(queued.pending).toBe(true);
    expect(applyPendingMarks(book, [])).toEqual([expect.objectContaining({ id: "offline-mark", pending: true })]);

    queueMarkDelete(book, queued);
    expect(applyPendingMarks(book, [])).toEqual([]);
  });

  test("local mode replays queued progress without a hosted CSRF cookie", async () => {
    localStorage.setItem("litlet.offline.owner", "local");
    vi.spyOn(document, "cookie", "get").mockReturnValue("");
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      bookmark: 2, position_epoch: 0,
    }), { status: 200, headers: { "content-type": "application/json" } }));
    queuePosition(book, `/api/books/${book}/position`, { cfi: "local", completed_chapter: 2 });

    await flushOfflineMutations();
    expect(fetch).toHaveBeenCalledWith(`/api/books/${book}/position`, expect.objectContaining({
      headers: expect.not.objectContaining({ "X-CSRF-Token": expect.anything() }),
    }));
    expect(pendingPosition(book)).toBeNull();
  });

  test("explicit offline save verifies the manifest and EPUB entered the owner cache", async () => {
    const stored = new Map<string, Response>();
    vi.stubGlobal("caches", {
      open: vi.fn(async () => ({
        put: async (path: string, response: Response) => { stored.set(path, response); },
        match: async (path: string) => stored.get(path),
      })),
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (path) => new Response(
      String(path).endsWith("/epub") ? new Uint8Array([1, 2, 3]) : "{}",
      { status: 200, headers: { "content-type": String(path).endsWith("/epub") ? "application/epub+zip" : "application/json" } },
    ));

    await expect(cacheBookForOffline(book)).resolves.toBeUndefined();
    expect(stored.has(`/api/books/${book}/manifest`)).toBe(true);
    expect(stored.has(`/api/books/${book}/epub`)).toBe(true);
  });

  test("purge removes every owner cache and all reader-local state", async () => {
    localStorage.setItem("rc:reader-marks:book", "secret note");
    localStorage.setItem("litlet.reader-preferences:book", "night");
    queuePosition(book, `/api/books/${book}/position`, { cfi: "queued" });
    const deleted: string[] = [];
    vi.stubGlobal("caches", {
      keys: vi.fn(async () => [`litlet-reader-owner-${owner}`, "litlet-shell-v1"]),
      delete: vi.fn(async (name: string) => { deleted.push(name); return true; }),
    });

    await purgeOfflineTenant(owner);
    expect(activeOfflineOwner()).toBe("local");
    expect(localStorage.getItem("rc:reader-marks:book")).toBeNull();
    expect(localStorage.getItem("litlet.reader-preferences:book")).toBeNull();
    expect(pendingPosition(book)).toBeNull();
    expect(deleted).toEqual([`litlet-reader-owner-${owner}`]);
  });
});
