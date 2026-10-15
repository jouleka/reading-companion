import { act, render, waitFor } from "@testing-library/react";
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
});
