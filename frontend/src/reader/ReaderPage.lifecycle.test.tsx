import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  manifest: vi.fn(),
  books: vi.fn(),
  position: vi.fn(),
  readerPreferences: vi.fn(),
  readerMarks: vi.fn(),
  putPosition: vi.fn(),
  putReaderPreferences: vi.fn(),
  epubUrl: vi.fn(() => "/api/books/b/epub"),
  readerMarksExportUrl: vi.fn(() => "/api/books/b/marks/export"),
  makeBook: vi.fn(),
  destroyBook: vi.fn(),
  open: vi.fn(),
  init: vi.fn(),
  goTo: vi.fn(),
  close: vi.fn(),
  search: vi.fn(),
  clearSearch: vi.fn(),
  onSearch: null as null | ((query: string) => Promise<Array<{ cfi: string }>>),
}));

vi.mock("../api", () => ({
  api: mocks,
  ApiError: class ApiError extends Error {
    constructor(
      message: string,
      readonly status: number,
    ) {
      super(message);
    }
  },
}));
vi.mock("../vendor/foliate-js/view.js", () => ({ makeBook: mocks.makeBook }));
vi.mock("./Companion", () => ({ Companion: () => null }));
vi.mock("./ReaderTTS", () => ({ ReaderTTS: () => null }));
vi.mock("./BookNavigation", () => ({
  ReaderNavigation: ({ onSearch }: { onSearch: typeof mocks.onSearch }) => {
    mocks.onSearch = onSearch;
    return null;
  },
}));

import { ReaderPage } from "./ReaderPage";
import { ApiError } from "../api";
import { pendingPosition } from "../pwa/offline";

class TestFoliateView extends HTMLElement {
  renderer = Object.assign(document.createElement("div"), {
    pages: 2,
    page: 1,
    setStyles: vi.fn(),
  });
  history = Object.assign(new EventTarget(), {
    canGoBack: false,
    canGoForward: false,
  });
  book = {
    sections: [{ id: "c1.html" }, { id: "c2.html" }, { id: "c3.html" }],
  };
  open = mocks.open;
  init = mocks.init;
  goTo = mocks.goTo;
  addAnnotation = vi.fn(async () => {});
  close = mocks.close;
  search = mocks.search;
  clearSearch = mocks.clearSearch;
  getTOCItemOf = async (cfi: string) => ({
    href: `c${Number(cfi.split(":")[0]) + 1}.html`,
  });
  resolveNavigation = (cfi: string) => ({ index: Number(cfi.split(":")[0]) });
}
customElements.define("foliate-view", TestFoliateView);

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  const sections = [{ id: "c1.html" }, { id: "c2.html" }, { id: "c3.html" }];
  mocks.manifest.mockResolvedValue({
    book_id: "b",
    mode: "file-driven",
    book_type: "novel",
    atoms: sections.map((section, index) => ({
      ordinal: index + 1,
      href: section.id,
      title: `Chapter ${index + 1}`,
      char_len: 100,
    })),
  });
  mocks.books.mockResolvedValue([{ book_id: "b", title: "A test book" }]);
  mocks.position.mockResolvedValue({
    bookmark: 1,
    cfi: "0:resume",
    position_epoch: 0,
  });
  mocks.readerPreferences.mockResolvedValue({});
  mocks.readerMarks.mockResolvedValue({ marks: [] });
  mocks.makeBook.mockResolvedValue({
    sections,
    toc: sections.map((section) => ({ href: section.id })),
    destroy: mocks.destroyBook,
  });
  mocks.open.mockResolvedValue(undefined);
  mocks.init.mockResolvedValue(undefined);
  mocks.goTo.mockResolvedValue(undefined);
  mocks.search.mockImplementation(async function* () {
    yield { cfi: "0:read", excerpt: { pre: "", match: "answer", post: "" } };
    yield { cfi: "2:future", excerpt: { pre: "", match: "answer", post: "" } };
  });
  mocks.onSearch = null;
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(new Blob(["epub"]), { status: 200 })),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("reader startup and search boundaries", () => {
  test.each([
    { bookmark: 1, target: "c2.html" },
    { bookmark: 3, target: "c3.html" },
    { bookmark: 30, target: "c3.html" },
  ])(
    "resumes legacy bookmark $bookmark at $target without counting front matter",
    async ({ bookmark, target }) => {
      mocks.position.mockResolvedValue({
        bookmark,
        cfi: null,
        position_epoch: 0,
      });
      mocks.makeBook.mockResolvedValue({
        sections: [
          "cover.html",
          "c1.html",
          "c2.html",
          "c3.html",
          "afterword.html",
        ].map((id) => ({ id })),
        destroy: mocks.destroyBook,
      });
      render(<ReaderPage bookId="b" onBack={() => {}} />);

      await waitFor(() =>
        expect(mocks.goTo).toHaveBeenCalledExactlyOnceWith(target),
      );
      expect(mocks.init).not.toHaveBeenCalled();
    },
  );

  test("keeps the first-section cover for a new book without a CFI", async () => {
    mocks.position.mockResolvedValue({
      bookmark: 0,
      cfi: null,
      position_epoch: 0,
    });
    mocks.makeBook.mockResolvedValue({
      sections: ["cover.html", "c1.html", "c2.html", "c3.html"].map((id) => ({
        id,
      })),
      destroy: mocks.destroyBook,
    });
    render(<ReaderPage bookId="b" onBack={() => {}} />);

    await waitFor(() => expect(mocks.goTo).toHaveBeenCalledExactlyOnceWith(0));
  });

  test("prefers an existing CFI over the legacy chapter bookmark", async () => {
    render(<ReaderPage bookId="b" onBack={() => {}} />);

    await waitFor(() =>
      expect(mocks.init).toHaveBeenCalledExactlyOnceWith({
        lastLocation: "0:resume",
      }),
    );
    expect(mocks.goTo).not.toHaveBeenCalled();
  });

  test.each([
    undefined,
    "",
    "missing.html",
    "https://example.test/chapter.html",
  ])("falls back safely when the legacy resume href is %s", async (href) => {
    mocks.position.mockResolvedValue({
      bookmark: 1,
      cfi: null,
      position_epoch: 0,
    });
    mocks.manifest.mockResolvedValue({
      book_id: "b",
      mode: "file-driven",
      book_type: "novel",
      atoms: [
        { ordinal: 1, href: "c1.html", title: "Chapter 1", char_len: 100 },
        { ordinal: 2, href, title: "Chapter 2", char_len: 100 },
      ],
    });
    mocks.makeBook.mockResolvedValue({
      sections: [
        "cover.html",
        "c1.html",
        "https://example.test/chapter.html",
      ].map((id) => ({ id })),
      destroy: mocks.destroyBook,
    });
    mocks.goTo.mockImplementationOnce(async () => {
      document.querySelector("foliate-view")?.dispatchEvent(
        new CustomEvent("relocate", {
          detail: { cfi: "cover:0", section: { current: 0 } },
        }),
      );
    });
    const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);

    await waitFor(() => expect(mocks.goTo).toHaveBeenCalledExactlyOnceWith(0));
    unmount();
    expect(mocks.putPosition).not.toHaveBeenCalled();
  });

  test("preserves a mapped fragment when legacy chapters share one EPUB file", async () => {
    mocks.position.mockResolvedValue({
      bookmark: 1,
      cfi: null,
      position_epoch: 0,
    });
    mocks.manifest.mockResolvedValue({
      book_id: "b",
      mode: "anchor-driven",
      book_type: "novel",
      atoms: [
        {
          ordinal: 1,
          href: "c1.html#first",
          title: "Chapter 1",
          char_len: 100,
        },
        {
          ordinal: 2,
          href: "c1.html#second",
          title: "Chapter 2",
          char_len: 100,
        },
      ],
    });
    mocks.makeBook.mockResolvedValue({
      sections: [{ id: "cover.html" }, { id: "c1.html" }],
      destroy: mocks.destroyBook,
    });
    render(<ReaderPage bookId="b" onBack={() => {}} />);

    await waitFor(() =>
      expect(mocks.goTo).toHaveBeenCalledExactlyOnceWith("c1.html#second"),
    );
  });

  test("starts the EPUB download while metadata is still pending", async () => {
    const metadata = deferred();
    mocks.books.mockImplementationOnce(async () => {
      await metadata.promise;
      return [];
    });
    const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(mocks.open).not.toHaveBeenCalled();
    await act(async () => {
      metadata.resolve();
    });
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    unmount();
    expect(mocks.destroyBook).toHaveBeenCalledOnce();
  });

  test.each(["open", "init"] as const)(
    "releases a book unmounted during engine %s",
    async (stage) => {
      const pending = deferred();
      mocks[stage].mockImplementationOnce(() => pending.promise);
      const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
      await waitFor(() => expect(mocks[stage]).toHaveBeenCalled());
      unmount();
      await act(async () => {
        pending.resolve();
      });
      expect(mocks.close).toHaveBeenCalledOnce();
      expect(mocks.destroyBook).toHaveBeenCalledOnce();
      if (stage === "open") expect(mocks.init).not.toHaveBeenCalled();
    },
  );

  test("aborts an unfinished EPUB download on unmount", async () => {
    let signal: AbortSignal | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, options: RequestInit) =>
          new Promise((_resolve, reject) => {
            signal = options.signal as AbortSignal;
            signal.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          }),
      ),
    );
    const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(signal).toBeDefined());
    await act(async () => {
      unmount();
    });
    expect(signal?.aborted).toBe(true);
    expect(mocks.makeBook).not.toHaveBeenCalled();
  });

  test("scans read sections only and rejects future candidates defensively", async () => {
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    let results: Array<{ cfi: string }> = [];
    await act(async () => {
      results = await mocks.onSearch!("answer");
    });
    expect(mocks.search).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({
        query: "answer",
        index: 0,
        draw: expect.any(Function),
      }),
    );
    expect(results.map((result) => result.cfi)).toEqual(["0:read"]);
    expect(mocks.clearSearch).toHaveBeenCalledOnce();
  });

  test("final-page landing stays pending until an explicit Finish book action is saved", async () => {
    mocks.position.mockResolvedValue({
      bookmark: 2,
      cfi: "2:resume",
      position_epoch: 0,
    });
    mocks.putPosition.mockResolvedValue({
      bookmark: 3,
      cfi: "2:final",
      position_epoch: 0,
      current_chapter: 4,
      chapter_progress: 1,
    });
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    const view = document.querySelector("foliate-view") as TestFoliateView;
    Object.assign(view.renderer, { page: 8, pages: 10 });
    act(() =>
      view.dispatchEvent(
        new CustomEvent("relocate", {
          detail: { cfi: "2:final", section: { current: 2 } },
        }),
      ),
    );
    expect(mocks.putPosition).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Finish book" }));
    await waitFor(() =>
      expect(mocks.putPosition).toHaveBeenCalledWith(
        "b",
        "2:final",
        300,
        3,
        0,
        0,
        expect.any(String),
        expect.any(Number),
      ),
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Finish book" })).toBeNull(),
    );
  });

  test("clears a final-page offer when moving to unmapped backmatter", async () => {
    mocks.position.mockResolvedValue({
      bookmark: 2,
      cfi: "2:resume",
      position_epoch: 0,
    });
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    const view = document.querySelector("foliate-view") as TestFoliateView;
    Object.assign(view.renderer, { page: 8, pages: 10 });
    act(() =>
      view.dispatchEvent(
        new CustomEvent("relocate", {
          detail: { cfi: "2:final", section: { current: 2 } },
        }),
      ),
    );
    expect(screen.getByRole("button", { name: "Finish book" })).toBeTruthy();
    act(() =>
      view.dispatchEvent(
        new CustomEvent("relocate", {
          detail: { cfi: "backmatter", section: { current: 3 } },
        }),
      ),
    );
    expect(screen.queryByRole("button", { name: "Finish book" })).toBeNull();
  });

  test("immediate unmount retains an in-flight completion and latest reread cursor without racing replay", async () => {
    mocks.position.mockResolvedValue({ bookmark: 2, cfi: "2:resume", position_epoch: 0 });
    let release!: (saved: unknown) => void;
    mocks.putPosition.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    const view = document.querySelector("foliate-view") as TestFoliateView;
    Object.assign(view.renderer, { page: 8, pages: 10 });
    act(() => view.dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "2:finished", section: { current: 2 } },
    })));
    fireEvent.click(screen.getByRole("button", { name: "Finish book" }));
    expect(mocks.putPosition).toHaveBeenCalledTimes(1);
    Object.assign(view.renderer, { page: 1, pages: 10 });
    act(() => view.dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "0:rereading", section: { current: 0 } },
    })));
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockImplementation(async (_url, options) => {
      expect(JSON.parse(options?.body as string)).toMatchObject({
        cfi: "0:rereading", offset: 0, completed_chapter: 3,
        high_water_offset: 300, position_epoch: 0,
      });
      return new Response(JSON.stringify({ bookmark: 3, position_epoch: 0 }), { status: 200 });
    });
    const callsBeforeUnmount = fetch.mock.calls.length;
    unmount();
    expect(pendingPosition("b")).toMatchObject({
      cfi: "0:rereading", offset: 0, completed_chapter: 3, high_water_offset: 300, position_epoch: 0,
    });
    await Promise.resolve();
    expect(fetch).toHaveBeenCalledTimes(callsBeforeUnmount);
    await act(async () => release({ bookmark: 3, cfi: "2:finished", position_epoch: 0,
      current_chapter: 4, chapter_progress: 1 }));
    await waitFor(() => expect(pendingPosition("b")).toBeNull());
    expect(fetch).toHaveBeenCalledTimes(callsBeforeUnmount + 1);
    expect(mocks.putPosition).toHaveBeenCalledTimes(1);
  });

  test("an ordinary queued offline position leaves explicit Finish available without refreshing locked memory", async () => {
    mocks.position.mockResolvedValue({ bookmark: 2, cfi: "2:resume", position_epoch: 0 });
    mocks.putPosition.mockResolvedValueOnce({ bookmark: 2, cfi: "2:final", position_epoch: 0,
      current_chapter: 3, chapter_progress: 0.8, queued: true })
      .mockResolvedValueOnce({ bookmark: 3, cfi: "2:final", position_epoch: 0,
        current_chapter: 4, chapter_progress: 1, queued: true });
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    const view = document.querySelector("foliate-view") as TestFoliateView;
    Object.assign(view.renderer, { page: 8, pages: 10 });
    act(() => view.dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "2:final", section: { current: 2 } },
    })));
    act(() => window.dispatchEvent(new Event("pagehide")));
    await waitFor(() => expect(mocks.putPosition).toHaveBeenCalledTimes(1));
    const finish = screen.getByRole("button", { name: "Finish book" }) as HTMLButtonElement;
    await waitFor(() => expect(finish.disabled).toBe(false));
    fireEvent.click(finish);
    await waitFor(() => expect(mocks.putPosition).toHaveBeenCalledWith(
      "b", "2:final", 300, 3, 0, expect.any(Number), expect.any(String), expect.any(Number),
    ));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Finish book" })).toBeNull());
    // Offline intent can update the local progress display, but cannot fetch newly unlocked labels
    // or facts until a canonical server acknowledgment reaches that frontier.
    expect(mocks.manifest).toHaveBeenCalledTimes(1);
    expect(mocks.readerMarks).toHaveBeenCalledTimes(1);
  });

  test("an epoch conflict cancels queued old-page intent and reloads from the new pass", async () => {
    mocks.position.mockResolvedValue({ bookmark: 0, cfi: null, position_epoch: 1 })
      .mockResolvedValueOnce({ bookmark: 2, cfi: "2:resume", position_epoch: 0 });
    let reject!: (error: Error) => void;
    mocks.putPosition.mockImplementation(() => new Promise((_resolve, fail) => { reject = fail; }));
    const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    const view = document.querySelector("foliate-view") as TestFoliateView;
    Object.assign(view.renderer, { page: 8, pages: 10 });
    act(() => view.dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "2:old-finish", section: { current: 2 } },
    })));
    fireEvent.click(screen.getByRole("button", { name: "Finish book" }));
    act(() => view.dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "0:old-reread", section: { current: 0 } },
    })));
    await act(async () => reject(new ApiError("position reset", 409)));
    await waitFor(() => expect(mocks.goTo).toHaveBeenCalledWith(0));
    expect(document.querySelectorAll("foliate-view")).toHaveLength(1);
    expect(mocks.putPosition).toHaveBeenCalledTimes(1);
    expect(mocks.putPosition.mock.calls[0][4]).toBe(0);
    expect(pendingPosition("b")).toBeNull();
    unmount();
    expect(mocks.putPosition).toHaveBeenCalledTimes(1);
  });

  test("reader cleanup never persists an old account's cursor under the newly active owner", async () => {
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    const view = document.querySelector("foliate-view") as TestFoliateView;
    act(() => view.dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "0:private-page", section: { current: 0 } },
    })));
    localStorage.setItem("litlet.offline.owner", "05dbebd1-e9e8-4be8-8172-eb65ca8d2a67");
    // pagehide exercises the same owner fence as unmount without requiring an account UI fixture.
    act(() => window.dispatchEvent(new Event("pagehide")));
    expect(pendingPosition("b")).toBeNull();
    expect(mocks.putPosition).not.toHaveBeenCalled();
  });
});
