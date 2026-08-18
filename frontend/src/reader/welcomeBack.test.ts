/** LIT-29 "since you were last here": a client-only return-gap detector (localStorage). On reopen
 * after a real break it surfaces the recap once with a welcome-back framing; any interaction steps it
 * out of the way. No backend — a presentation moment over the same flowing recap. */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test } from "vitest";
import { WELCOME_BACK_GAP_MS, useWelcomeBack } from "./welcomeBack";

const KEY = "rc:lastSeen:b";
const setPrior = (msAgo: number, bm = 2) =>
  localStorage.setItem(KEY, JSON.stringify({ t: Date.now() - msAgo, bm }));

beforeEach(() => localStorage.clear());
afterEach(() => localStorage.clear());

describe("useWelcomeBack", () => {
  test("surfaces after a gap beyond the threshold, framed to where you left off", () => {
    setPrior(WELCOME_BACK_GAP_MS + 60_000);
    const { result } = renderHook(() => useWelcomeBack("b", 3));
    expect(result.current.welcomeBack).toEqual({ lastChapter: 3 });
  });

  test("does not surface within the threshold (a short break)", () => {
    setPrior(60 * 60 * 1000); // 1h ago
    const { result } = renderHook(() => useWelcomeBack("b", 3));
    expect(result.current.welcomeBack).toBeNull();
  });

  test("does not surface on a first-ever visit (no prior record)", () => {
    const { result } = renderHook(() => useWelcomeBack("b", 3));
    expect(result.current.welcomeBack).toBeNull();
  });

  test("records the visit so a later reopen measures the gap from now", () => {
    renderHook(() => useWelcomeBack("b", 3));
    const rec = JSON.parse(localStorage.getItem(KEY)!);
    expect(rec.bm).toBe(3);
    expect(Date.now() - rec.t).toBeLessThan(5000);
  });

  test("dismiss steps it out of the way", () => {
    setPrior(WELCOME_BACK_GAP_MS + 60_000);
    const { result } = renderHook(() => useWelcomeBack("b", 3));
    expect(result.current.welcomeBack).not.toBeNull();
    act(() => result.current.dismiss());
    expect(result.current.welcomeBack).toBeNull();
  });

  test("surfaces at most once — a page turn (bookmark change) dismisses it", () => {
    setPrior(WELCOME_BACK_GAP_MS + 60_000);
    const { result, rerender } = renderHook(({ bm }) => useWelcomeBack("b", bm), {
      initialProps: { bm: 3 },
    });
    expect(result.current.welcomeBack).toEqual({ lastChapter: 3 });
    rerender({ bm: 4 });
    expect(result.current.welcomeBack).toBeNull();
  });

  test("a not-yet-loaded (null) bookmark never surfaces", () => {
    setPrior(WELCOME_BACK_GAP_MS + 60_000);
    const { result } = renderHook(() => useWelcomeBack("b", null));
    expect(result.current.welcomeBack).toBeNull();
  });
});
