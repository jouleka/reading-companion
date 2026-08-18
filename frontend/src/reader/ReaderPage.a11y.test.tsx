/** LIT-16: the reader shell is keyboard-navigable and correctly landmarked. The foliate engine and
 * the network are mocked so the static shell (skip link, main, running head, page controls) renders
 * deterministically without a real book. */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({
  manifest: vi.fn(),
  books: vi.fn(),
  position: vi.fn(),
  readerPreferences: vi.fn(),
  putReaderPreferences: vi.fn(),
  putPosition: vi.fn(),
  resetPosition: vi.fn(),
  readerMarks: vi.fn(),
  readerMarksExportUrl: vi.fn(() => "/api/books/b/marks/export"),
  createHighlight: vi.fn(),
  createAnnotation: vi.fn(),
  createBookmark: vi.fn(),
  deleteReaderMark: vi.fn(),
  ingest: vi.fn(),
  catchMeUp: vi.fn(),
  epubUrl: vi.fn(() => "/api/books/b/epub"),
}));
vi.mock("../api", () => ({
  api: mocks,
  ApiError: class ApiError extends Error {
    constructor(
      message: string,
      readonly status: number,
      readonly code?: string,
    ) {
      super(message);
    }
  },
}));
vi.mock("../vendor/foliate-js/view.js", () => ({ makeBook: vi.fn(async () => ({ sections: [] })) }));

import { ReaderPage } from "./ReaderPage";

const atoms = [
  { ordinal: 1, href: "c1.html", title: "Chapter I", part_label: "", char_len: 1000 },
  { ordinal: 2, href: "c2.html", title: "", part_label: "", char_len: 1000 },
];

beforeEach(() => {
  localStorage.clear(); // isolate the LIT-29 welcome-back gap tracking between tests
  mocks.manifest.mockResolvedValue({ book_id: "b", atom_set_version: "v", mode: "file-driven", atoms });
  mocks.books.mockResolvedValue([{ book_id: "b", title: "The Test Book", author: "A. Author" }]);
  mocks.position.mockResolvedValue({
    bookmark: 1, cfi: null, ingest_progress: 1, atoms: 2, position_epoch: 0,
  });
  mocks.readerPreferences.mockResolvedValue({
    font_size: "book", line_height: "comfortable", measure: "balanced", theme: "paper",
    margins: "balanced", typeface: "publisher", preference_version: 0,
  });
  mocks.putReaderPreferences.mockImplementation(async (_id, preferences) => ({
    ...preferences, preference_version: 1,
  }));
  mocks.resetPosition.mockResolvedValue({
    bookmark: 0, cfi: null, ingest_progress: 1, atoms: 2, position_epoch: 1,
  });
  mocks.ingest.mockResolvedValue({ ingest_progress: 1, status: "idle", flags: [], error: null });
  mocks.catchMeUp.mockResolvedValue({ recap: null, as_of_chapter: 1, cast_size: 0, open_threads: 0, cached: false });
  global.fetch = vi.fn(async () => new Response(new Blob(["x"]), { status: 200 })) as unknown as typeof fetch;
});
afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("ReaderPage a11y shell", () => {
  test("exposes a main landmark and a skip link that targets it", async () => {
    const { container } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.manifest).toHaveBeenCalled());
    const skip = container.querySelector("a.skip-link") as HTMLAnchorElement;
    expect(skip).not.toBeNull();
    const main = container.querySelector("main#main");
    expect(main).not.toBeNull();
    expect(skip.getAttribute("href")).toBe("#main");
  });

  test("page controls are buttons with accessible names", async () => {
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.manifest).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: /previous page/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /next page/i })).toBeTruthy();
  });

  test("a marks-service failure never prevents the book shell from opening", async () => {
    mocks.readerMarks.mockRejectedValueOnce(new Error("marks offline"));
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    expect(await screen.findByRole("heading", { level: 1, name: /the test book/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /next page/i })).toBeTruthy();
  });
  mocks.readerMarks.mockResolvedValue({ as_of_chapter: 1, marks: [] });

  test("uses the collapsed companion sheet at a phone viewport while keeping the book landmark", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: true,
      media: "(max-width: 900px)",
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })));
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await screen.findByRole("heading", { level: 1, name: /the test book/i });
    const book = screen.getByRole("main");
    const toggle = screen.getByRole("button", { name: /open reading companion/i });
    fireEvent.click(toggle);
    expect(screen.getByRole("main")).toBe(book);
    expect(screen.getByRole("complementary", { name: /companion/i })
      .getAttribute("data-expanded")).toBe("true");
  });

  test("applies appearance presets immediately and syncs the whole preference object", async () => {
    const { container } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    await screen.findByRole("heading", { level: 1, name: /the test book/i });
    fireEvent.click(screen.getByRole("button", { name: /reading appearance/i }));
    fireEvent.click(screen.getByRole("radio", { name: /night/i }));
    expect(container.querySelector(".reader-grid")?.getAttribute("data-reader-theme")).toBe("night");
    await waitFor(() => expect(mocks.putReaderPreferences).toHaveBeenCalledWith(
      "b",
      expect.objectContaining({ theme: "night", font_size: "book", measure: "balanced" }),
    ));
  });

  test("the book title is the page's h1", async () => {
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await screen.findByRole("heading", { level: 1, name: /the test book/i });
  });

  test("no axe violations in the reader shell", async () => {
    const { container } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.manifest).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByRole("button", { name: /next page/i })).toBeTruthy());
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("LIT-29: a return after a gap auto-surfaces the story-so-far with a welcome-back framing", async () => {
    // last seen 5h ago at chapter 1 -> beyond the ~4h threshold
    localStorage.setItem("rc:lastSeen:b", JSON.stringify({ t: Date.now() - 5 * 60 * 60 * 1000, bm: 1 }));
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    const banner = (await screen.findByText(/welcome back/i)).closest(".welcome-back")!;
    expect(banner.textContent).toMatch(/left off in chapter I\b/i);
  });

  test("LIT-29: no welcome-back on a fresh first visit (nothing auto-opens)", async () => {
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.manifest).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByRole("button", { name: /next page/i })).toBeTruthy());
    expect(screen.queryByText(/welcome back/i)).toBeNull();
  });

  test("LIT-17: starting a new pass is confirmed and uses the current position epoch", async () => {
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    const trigger = await screen.findByRole("button", { name: /start over/i });
    fireEvent.click(trigger);
    expect(screen.getByRole("alertdialog").textContent).toMatch(/companion.*nothing read/i);
    fireEvent.click(screen.getByRole("button", { name: /start again/i }));
    await waitFor(() => expect(mocks.resetPosition).toHaveBeenCalledWith("b", 0));
    expect(localStorage.getItem("rc:lastSeen:b")).toBeNull();
  });
});
