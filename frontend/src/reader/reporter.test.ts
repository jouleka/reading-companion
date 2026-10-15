/** The debounced position reporter. The original inline debounce DISCARDED the pending PUT on
 * unmount — a reader who paged and immediately left lost their final position (the product's one
 * promise). The reporter must coalesce bursts AND flush the pending report when asked. */
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { PositionReporter, type PendingPosition } from "./reporter";

describe("PositionReporter", () => {
  let sent: Array<{ cfi: string; offset: number }>;
  let r: PositionReporter;

  beforeEach(() => {
    vi.useFakeTimers();
    sent = [];
    r = new PositionReporter((cfi, offset) => {
      sent.push({ cfi, offset });
    }, 700);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  test("delivers after the debounce delay", () => {
    r.schedule("cfi-1", 10);
    expect(sent).toEqual([]);
    vi.advanceTimersByTime(700);
    expect(sent).toEqual([{ cfi: "cfi-1", offset: 10 }]);
  });

  test("a burst coalesces to the LAST report", () => {
    r.schedule("cfi-1", 10);
    vi.advanceTimersByTime(300);
    r.schedule("cfi-2", 20);
    vi.advanceTimersByTime(700);
    expect(sent).toEqual([{ cfi: "cfi-2", offset: 20 }]);
  });

  test("flush() delivers the pending report immediately (the unmount path)", () => {
    r.schedule("cfi-1", 10);
    r.flush();
    expect(sent).toEqual([{ cfi: "cfi-1", offset: 10 }]);
    vi.advanceTimersByTime(2000);
    expect(sent).toHaveLength(1); // never twice
  });

  test("flush() with nothing pending is a no-op", () => {
    r.flush();
    r.schedule("cfi-1", 10);
    vi.advanceTimersByTime(700);
    r.flush();
    expect(sent).toEqual([{ cfi: "cfi-1", offset: 10 }]);
  });

  test("cancel() discards a pre-reset report so it cannot race a new reading pass", () => {
    r.schedule("old-pass", 9000);
    r.cancel();
    vi.advanceTimersByTime(2000);
    r.flush();
    expect(sent).toEqual([]);
  });

  test("retains a failed delivery and retries it after bounded backoff", async () => {
    const states: string[] = [];
    let attempts = 0;
    r = new PositionReporter(
      async (cfi, offset) => {
        attempts += 1;
        if (attempts === 1) throw new TypeError("offline");
        sent.push({ cfi, offset });
      },
      700,
      (state) => states.push(state),
    );
    r.schedule("survives-reconnect", 42, 3);
    await vi.advanceTimersByTimeAsync(700);
    expect(attempts).toBe(1);
    expect(states.at(-1)).toBe("queued");
    await vi.advanceTimersByTimeAsync(1_000);
    expect(attempts).toBe(2);
    expect(sent).toEqual([{ cfi: "survives-reconnect", offset: 42 }]);
    expect(states.at(-1)).toBe("saved");
  });

  test("serializes writes and sends the newest relocation after an in-flight write", async () => {
    const calls: string[] = [];
    let releaseFirst: (() => void) | undefined;
    r = new PositionReporter(async (cfi) => {
      calls.push(cfi);
      if (cfi === "first")
        await new Promise<void>((resolve) => {
          releaseFirst = resolve;
        });
    }, 700);

    r.schedule("first", 10, 0);
    await vi.advanceTimersByTimeAsync(700);
    r.schedule("latest", 30, 1);
    await vi.advanceTimersByTimeAsync(700);
    expect(calls).toEqual(["first"]);
    releaseFirst?.();
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toEqual(["first", "latest"]);
  });

  test("reports a server merge conflict without retrying a successful response", async () => {
    const states: string[] = [];
    r = new PositionReporter(
      async (): Promise<"conflict"> => "conflict",
      10,
      (state) => states.push(state),
    );
    r.schedule("behind", 1, 0);
    await vi.advanceTimersByTimeAsync(10);
    expect(states.at(-1)).toBe("conflict");
  });

  test("keeps the queued state returned by a durable offline delivery", async () => {
    const states: string[] = [];
    r = new PositionReporter(
      async (): Promise<"queued"> => "queued",
      10,
      (state) => states.push(state),
    );
    r.schedule("offline", 8, 2);
    await vi.advanceTimersByTimeAsync(10);
    expect(states.at(-1)).toBe("queued");
  });

  test("serializes an explicit completion ahead of later relocations", async () => {
    const calls: string[] = [];
    let release!: () => void;
    r = new PositionReporter(async (cfi) => {
      calls.push(cfi);
      if (cfi === "first")
        await new Promise<void>((resolve) => {
          release = resolve;
        });
    });
    r.schedule("first", 10, 0);
    r.flush();
    r.complete("finished", 300, 3);
    r.schedule("rereading", 100, 1);
    expect(calls).toEqual(["first"]);
    release();
    await vi.advanceTimersByTimeAsync(1);
    expect(calls).toEqual(["first", "finished", "rereading"]);
  });

  test("a failed completion survives page turns and a reset cancels it", async () => {
    const calls: string[] = [];
    let fail = true;
    r = new PositionReporter(async (cfi) => {
      calls.push(cfi);
      if (fail) {
        fail = false;
        throw new TypeError("offline");
      }
    });
    r.complete("finished", 300, 3);
    await Promise.resolve();
    r.schedule("rereading", 100, 1);
    await vi.advanceTimersByTimeAsync(701);
    expect(calls).toEqual(["finished", "finished", "rereading"]);
    r.complete("old-finish", 300, 3);
    r.cancel();
    await vi.advanceTimersByTimeAsync(2000);
    expect(calls.filter((cfi) => cfi === "old-finish")).toHaveLength(1);
  });

  test("teardown retains an in-flight write, queued completion and latest cursor before disposal", async () => {
    const retained: PendingPosition[] = [];
    const calls: string[] = [];
    let release!: () => void;
    r = new PositionReporter(async (cfi) => {
      calls.push(cfi);
      await new Promise<void>((resolve) => { release = resolve; });
    });
    r.schedule("first", 100, 1, 4);
    r.flush();
    r.complete("finished", 300, 3, 4);
    r.schedule("rereading", 10, 0, 4);
    let drained = false;
    const drain = r.drainAndDispose((report) => retained.push({ ...report })).then(() => { drained = true; });
    expect(retained).toEqual([
      { cfi: "first", offset: 100, completedChapter: 1, positionEpoch: 4 },
      { cfi: "finished", offset: 300, completedChapter: 3, positionEpoch: 4 },
      { cfi: "rereading", offset: 10, completedChapter: 0, positionEpoch: 4 },
    ]);
    await vi.advanceTimersByTimeAsync(2000);
    expect(calls).toEqual(["first"]);
    expect(drained).toBe(false);
    release();
    await drain;
    expect(drained).toBe(true);
    expect(calls).toEqual(["first"]);
  });

  test("a reset excludes old intent from retention and keeps the next epoch serialized", async () => {
    const calls: Array<[string, number]> = [];
    let release!: () => void;
    r = new PositionReporter(async (cfi, _offset, _completed, epoch) => {
      calls.push([cfi, epoch]);
      if (epoch === 0) await new Promise<void>((resolve) => { release = resolve; });
    });
    r.complete("old-finish", 300, 3, 0);
    r.cancel();
    r.schedule("new-pass", 0, 0, 1);
    const retained: PendingPosition[] = [];
    r.retain((report) => retained.push({ ...report }));
    r.flush();
    expect(calls).toEqual([["old-finish", 0]]);
    expect(retained).toEqual([{ cfi: "new-pass", offset: 0, completedChapter: 0, positionEpoch: 1 }]);
    release();
    await vi.advanceTimersByTimeAsync(1);
    expect(calls).toEqual([["old-finish", 0], ["new-pass", 1]]);
  });

  test("throwing persistence never aborts synchronous cleanup or leaves reporter timers active", async () => {
    const quota = new DOMException("storage full", "QuotaExceededError");
    r.schedule("last-page", 20, 1, 0);
    let drain!: Promise<void>;
    expect(() => {
      drain = r.drainAndDispose(() => { throw quota; });
    }).not.toThrow();
    expect(sent).toEqual([{ cfi: "last-page", offset: 20 }]);
    await expect(drain).rejects.toBe(quota);
    r.schedule("after-dispose", 30, 1, 0);
    r.flush();
    await vi.advanceTimersByTimeAsync(2000);
    expect(sent).toEqual([{ cfi: "last-page", offset: 20 }]);
  });
});
