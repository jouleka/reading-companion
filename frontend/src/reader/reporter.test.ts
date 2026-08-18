/** The debounced position reporter. The original inline debounce DISCARDED the pending PUT on
 * unmount — a reader who paged and immediately left lost their final position (the product's one
 * promise). The reporter must coalesce bursts AND flush the pending report when asked. */
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { PositionReporter } from "./reporter";

describe("PositionReporter", () => {
  let sent: Array<{ cfi: string; offset: number }>;
  let r: PositionReporter;

  beforeEach(() => {
    vi.useFakeTimers();
    sent = [];
    r = new PositionReporter((cfi, offset) => { sent.push({ cfi, offset }); }, 700);
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
      if (cfi === "first") await new Promise<void>((resolve) => { releaseFirst = resolve; });
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
});
