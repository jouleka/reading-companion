/** The companion SIDEBAR (LIT-29): a tight orientation — chapter X of N, the cast count, and the
 * generated 'right now' one-liner — DIFFERENTIATED from the hero's flowing recap (which it no longer
 * duplicates). Plus the honesty properties carried from before: the async tick dies with its effect,
 * the failure copy promises no retry that does not exist, and a one-liner behind the bookmark says so. */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({
  ingest: vi.fn(),
  catchMeUp: vi.fn(),
}));
vi.mock("../api", () => ({
  api: { ingest: mocks.ingest, catchMeUp: mocks.catchMeUp },
}));

import { Companion } from "./Companion";

const ingestDone = {
  ingest_progress: 96,
  status: "idle",
  flags: [],
  error: null,
};
const cmu = (as_of: number, over: Record<string, unknown> = {}) => ({
  recap: "Much has happened in the valley.",
  now: "Aldric and Berenice have just met at the forge.",
  as_of_chapter: as_of,
  cast_size: 4,
  open_threads: 2,
  cast: ["Aldric", "Berenice"],
  cached: false,
  ...over,
});

beforeEach(() => {
  mocks.ingest.mockReset();
  mocks.catchMeUp.mockReset();
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("Companion", () => {
  test("shows the tight 'right now' one-liner, NOT the full flowing recap (surfaces differ)", async () => {
    mocks.ingest.mockResolvedValue(ingestDone);
    mocks.catchMeUp.mockResolvedValue(cmu(3));
    render(<Companion bookId="b" bookmark={3} totalAtoms={96} />);
    expect(await screen.findByText(/just met at the forge/i)).toBeTruthy(); // the one-liner
    expect(screen.queryByText(/Much has happened in the valley/)).toBeNull(); // the hero recap is NOT here
  });

  test("orientation line names the chapter and the count", async () => {
    mocks.ingest.mockResolvedValue(ingestDone);
    mocks.catchMeUp.mockResolvedValue(cmu(3));
    render(<Companion bookId="b" bookmark={3} totalAtoms={96} />);
    await screen.findByText(/just met at the forge/i);
    expect(screen.getByText(/as of chapter III · 3 of 96/)).toBeTruthy();
    expect(screen.getByText(/cast so far/i)).toBeTruthy();
  });

  test("reference books use neutral section language and hide empty novel furniture", async () => {
    mocks.ingest.mockResolvedValue(ingestDone);
    mocks.catchMeUp.mockResolvedValue(
      cmu(3, { cast_size: 0, open_threads: 0 }),
    );
    render(
      <Companion
        bookId="b"
        bookmark={3}
        totalAtoms={12}
        bookType="reference"
        onOpenHero={() => {}}
        onOpenCodex={() => {}}
      />,
    );
    await screen.findByText(/just met at the forge/i);
    expect(screen.getByText(/as of section III/i)).toBeTruthy();
    expect(screen.queryByText(/cast so far/i)).toBeNull();
    expect(screen.queryByText(/open threads/i)).toBeNull();
    expect(
      screen.getByRole("button", { name: /what you've read/i }),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: /reading notes/i })).toBeTruthy();
  });

  test("a rejected recap offers explicit retry without promising a next-page refresh", async () => {
    mocks.ingest.mockResolvedValue(ingestDone);
    mocks.catchMeUp.mockRejectedValue(new Error("502"));
    render(<Companion bookId="b" bookmark={3} totalAtoms={96} />);
    await waitFor(() => expect(mocks.catchMeUp).toHaveBeenCalled());
    expect(
      await screen.findByText(/did not pass the spoiler gate/i),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Retry companion" }),
    ).toBeTruthy();
    expect(
      screen.queryByText(/retry on your next page|retry shortly/i),
    ).toBeNull();
  });

  test("a rejected recap does not hide the structured codex", async () => {
    const onOpenCodex = vi.fn();
    mocks.ingest.mockResolvedValue(ingestDone);
    mocks.catchMeUp.mockRejectedValue(new Error("502"));
    render(
      <Companion
        bookId="b"
        bookmark={3}
        totalAtoms={96}
        onOpenCodex={onOpenCodex}
      />,
    );
    await screen.findByText(/did not pass the spoiler gate/i);
    fireEvent.click(screen.getByRole("button", { name: /people & chapters/i }));
    expect(onOpenCodex).toHaveBeenCalledTimes(1);
  });

  test("offers a closeout for the latest completed chapter", async () => {
    const onOpenCloseout = vi.fn();
    mocks.ingest.mockResolvedValue(ingestDone);
    mocks.catchMeUp.mockResolvedValue(cmu(3));
    render(
      <Companion
        bookId="b"
        bookmark={3}
        totalAtoms={96}
        onOpenCloseout={onOpenCloseout}
      />,
    );
    await screen.findByText(/just met at the forge/i);
    fireEvent.click(
      screen.getByRole("button", { name: /close out chapter iii/i }),
    );
    expect(onOpenCloseout).toHaveBeenCalledTimes(1);
  });

  test("workspace reflection readiness follows preparation at the current bookmark", async () => {
    const onReady = vi.fn();
    mocks.ingest.mockResolvedValue({ ...ingestDone, ingest_progress: 3 });
    mocks.catchMeUp.mockResolvedValue(cmu(3));
    const { rerender } = render(<Companion bookId="b" bookmark={3} totalAtoms={96} onCloseoutReadinessChange={onReady} />);
    await waitFor(() => expect(onReady).toHaveBeenLastCalledWith(3));
    rerender(<Companion bookId="b" bookmark={4} totalAtoms={96} onCloseoutReadinessChange={onReady} />);
    await waitFor(() => expect(onReady).toHaveBeenLastCalledWith(null));
  });

  test("keeps the Codex available and reports rejected provider credentials truthfully", async () => {
    const onOpenCodex = vi.fn();
    mocks.ingest.mockResolvedValue(ingestDone);
    mocks.catchMeUp.mockRejectedValue(
      Object.assign(new Error("provider unavailable"), {
        code: "provider_authentication_failed",
        status: 503,
      }),
    );
    render(
      <Companion
        bookId="b"
        bookmark={3}
        totalAtoms={96}
        onOpenCodex={onOpenCodex}
      />,
    );
    expect(
      await screen.findByText(
        /AI companion is offline.*credentials were rejected/i,
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/spoiler gate/i)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /people & chapters/i }));
    expect(onOpenCodex).toHaveBeenCalledTimes(1);
  });

  test("a one-liner that lags the bookmark is attributed to ITS chapter, not the bookmark's", async () => {
    mocks.ingest.mockResolvedValue({
      ingest_progress: 2,
      status: "blocked",
      flags: ["gate"],
      error: null,
    });
    mocks.catchMeUp.mockResolvedValue(cmu(2));
    render(<Companion bookId="b" bookmark={5} totalAtoms={96} />);
    await screen.findByText(/just met at the forge/i);
    // the one-liner carries its own as-of (II); the position line keeps the bookmark (V)
    expect(screen.getByText(/through chapter II/i)).toBeTruthy();
    expect(screen.getByText(/as of chapter V/)).toBeTruthy();
  });

  test("an in-flight tick that resolves after unmount does not continue the chain", async () => {
    let resolveIngest!: (v: unknown) => void;
    mocks.ingest.mockReturnValue(
      new Promise((res) => {
        resolveIngest = res;
      }),
    );
    mocks.catchMeUp.mockResolvedValue(cmu(3));
    const { unmount } = render(
      <Companion bookId="b" bookmark={3} totalAtoms={96} />,
    );
    await waitFor(() => expect(mocks.ingest).toHaveBeenCalled());
    unmount();
    resolveIngest(ingestDone); // the poll would now go terminal and fetch the recap
    await new Promise((r) => setTimeout(r, 20));
    expect(mocks.catchMeUp).not.toHaveBeenCalled();
  });

  test("one failed ingest poll does not kill the chain — it retries and the one-liner still arrives", async () => {
    mocks.ingest
      .mockRejectedValueOnce(new Error("blip"))
      .mockResolvedValue(ingestDone);
    mocks.catchMeUp.mockResolvedValue(cmu(3));
    render(<Companion bookId="b" bookmark={3} totalAtoms={96} />);
    await screen.findByText(/just met at the forge/i, undefined, {
      timeout: 4000,
    });
    expect(mocks.ingest.mock.calls.length).toBeGreaterThanOrEqual(2);
  }, 10000);

  test("exhausted polling shows an unavailable state and retries at the same bookmark", async () => {
    vi.useFakeTimers();
    mocks.ingest.mockRejectedValue(new Error("Connection unavailable"));
    mocks.catchMeUp.mockResolvedValue(cmu(3));
    render(<Companion bookId="b" bookmark={3} totalAtoms={96} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8 * 1200);
    });
    expect(mocks.ingest).toHaveBeenCalledTimes(9);
    expect(
      screen.getByText("Your reading memory is temporarily unavailable."),
    ).toBeTruthy();
    expect(screen.queryByText("Reading your chapters…")).toBeNull();
    expect(mocks.catchMeUp).not.toHaveBeenCalled();
    mocks.ingest.mockResolvedValue(ingestDone);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry companion" }));
    });
    expect(mocks.ingest).toHaveBeenCalledTimes(10);
    expect(mocks.catchMeUp).toHaveBeenCalledWith("b", 3);
    expect(screen.getByText(/just met at the forge/i)).toBeTruthy();
    expect(
      screen.queryByText(/updates are temporarily unavailable/i),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "Refresh companion" }),
    ).toBeTruthy();
  });

  test("failed refresh retains the last safe recap with its original chapter attribution", async () => {
    vi.useFakeTimers();
    mocks.ingest.mockResolvedValue(ingestDone);
    mocks.catchMeUp.mockResolvedValue(cmu(2));
    const { rerender } = render(
      <Companion bookId="b" bookmark={2} totalAtoms={96} />,
    );
    await act(async () => {});
    mocks.ingest.mockRejectedValue(new Error("Connection unavailable"));
    rerender(<Companion bookId="b" bookmark={5} totalAtoms={96} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8 * 1200);
    });
    expect(screen.getByText(/just met at the forge/i)).toBeTruthy();
    expect(screen.getByText(/through chapter II/i)).toBeTruthy();
    expect(screen.getByText(/as of chapter V/i)).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Retry companion" }),
    ).toBeTruthy();
    expect(
      screen.getByText(/updates are temporarily unavailable/i),
    ).toBeTruthy();
    // Starting a fresh pass must hide the older snapshot immediately, even if the service is down.
    rerender(<Companion bookId="b" bookmark={0} totalAtoms={96} />);
    expect(screen.queryByText(/just met at the forge/i)).toBeNull();
  });

  test("bounded polling that is still behind shows updating with an explicit check-again action", async () => {
    vi.useFakeTimers();
    mocks.ingest.mockResolvedValue({
      ingest_progress: 1,
      status: "working",
      flags: [],
      error: null,
    });
    mocks.catchMeUp.mockResolvedValue(cmu(1));
    render(<Companion bookId="b" bookmark={3} totalAtoms={96} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8 * 1200);
    });
    expect(screen.getByText(/The companion is still updating/i)).toBeTruthy();
    expect(screen.getByText(/through chapter I ·/i)).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Retry companion" }),
    ).toBeTruthy();
    expect(screen.queryByText("Reading your chapters…")).toBeNull();
  });

  test("the one-liner arrives inside a polite live region (a screen reader announces it)", async () => {
    mocks.ingest.mockResolvedValue(ingestDone);
    mocks.catchMeUp.mockResolvedValue(cmu(3));
    const { container } = render(
      <Companion bookId="b" bookmark={3} totalAtoms={96} />,
    );
    await screen.findByText(/just met at the forge/i);
    const region = container.querySelector('[aria-live="polite"]');
    expect(region).not.toBeNull();
    expect(region!.textContent).toMatch(/just met at the forge/i);
  });

  test("the companion is a labelled complementary landmark with no axe violations", async () => {
    mocks.ingest.mockResolvedValue(ingestDone);
    mocks.catchMeUp.mockResolvedValue(cmu(3));
    const { container } = render(
      <Companion bookId="b" bookmark={3} totalAtoms={96} />,
    );
    await screen.findByText(/just met at the forge/i);
    expect(
      screen.getByRole("complementary", { name: /companion/i }),
    ).toBeTruthy();
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("mobile companion is a collapsed bottom sheet that opens without hiding the book", async () => {
    mocks.ingest.mockResolvedValue(ingestDone);
    mocks.catchMeUp.mockResolvedValue(cmu(3));
    const { container } = render(
      <Companion bookId="b" bookmark={3} totalAtoms={96} compact />,
    );
    const sheet = screen.getByRole("complementary", { name: /companion/i });
    const toggle = screen.getByRole("button", {
      name: /open reading companion/i,
    });
    expect(sheet.getAttribute("data-expanded")).toBe("false");
    expect(
      container.querySelector(".companion-body")?.hasAttribute("hidden"),
    ).toBe(true);
    fireEvent.click(toggle);
    expect(sheet.getAttribute("data-expanded")).toBe("true");
    expect(
      container.querySelector(".companion-body")?.hasAttribute("hidden"),
    ).toBe(false);
    expect(
      await screen.findByRole("heading", { name: /where you stand/i }),
    ).toBe(document.activeElement);
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("Escape closes the mobile sheet and restores focus to its handle", async () => {
    mocks.ingest.mockResolvedValue(ingestDone);
    mocks.catchMeUp.mockResolvedValue(cmu(3));
    render(<Companion bookId="b" bookmark={3} totalAtoms={96} compact />);
    await waitFor(() => expect(mocks.catchMeUp).toHaveBeenCalled());
    const toggle = screen.getByRole("button", {
      name: /open reading companion/i,
    });
    fireEvent.click(toggle);
    const sheet = screen.getByRole("complementary", { name: /companion/i });
    fireEvent.keyDown(sheet, { key: "Escape" });
    expect(
      screen.getByRole("button", { name: /open reading companion/i }),
    ).toBe(document.activeElement);
    expect(sheet.getAttribute("data-expanded")).toBe("false");
  });

  test("a bookmark change mid-flight abandons the old chain (no duplicate recap fetches)", async () => {
    let resolveOld!: (v: unknown) => void;
    mocks.ingest
      .mockReturnValueOnce(
        new Promise((res) => {
          resolveOld = res;
        }),
      )
      .mockResolvedValue(ingestDone);
    mocks.catchMeUp.mockResolvedValue(cmu(4));
    const { rerender } = render(
      <Companion bookId="b" bookmark={3} totalAtoms={96} />,
    );
    await waitFor(() => expect(mocks.ingest).toHaveBeenCalledTimes(1));
    rerender(<Companion bookId="b" bookmark={4} totalAtoms={96} />);
    resolveOld(ingestDone); // the stale chain resolving must not fetch a second recap
    await waitFor(() => expect(mocks.catchMeUp).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 20));
    expect(mocks.catchMeUp).toHaveBeenCalledTimes(1);
  });
});
