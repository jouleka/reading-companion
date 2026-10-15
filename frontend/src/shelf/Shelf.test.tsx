/** Shelf import-failure behavior: the error strip shows the server's human message (not raw
 * status + JSON), and the file input resets so re-selecting the same file retries. */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({
  books: vi.fn(),
  importBook: vi.fn(),
  providerSettings: vi.fn(),
}));
vi.mock("../api", () => ({
  api: {
    books: mocks.books,
    importBook: mocks.importBook,
    providerSettings: mocks.providerSettings,
  },
}));

import { Shelf } from "./Shelf";

beforeEach(() => {
  localStorage.clear();
  mocks.books.mockReset().mockResolvedValue([]);
  mocks.importBook.mockReset();
  mocks.providerSettings.mockReset().mockRejectedValue(new Error("local mode"));
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function selectFile(input: HTMLInputElement) {
  const f = new File([new Uint8Array([1, 2, 3])], "junk.epub", {
    type: "application/epub+zip",
  });
  fireEvent.change(input, { target: { files: [f] } });
}

describe("Shelf import failure", () => {
  test("shows the server's message, not raw status + JSON", async () => {
    mocks.importBook.mockRejectedValue(
      new Error("not a readable EPUB: BadZipFile"),
    );
    const { container } = render(<Shelf onOpen={() => {}} />);
    const input = container.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    selectFile(input);
    await screen.findByText(/not a readable EPUB/);
    // no raw Error-toString scaffolding in the user-facing strip
    expect(screen.queryByText(/^⚠ Error:/)).toBeNull();
  });

  // NOTE: the input-value reset (re-selecting the SAME file must re-fire onChange) is not
  // representable in jsdom — a file input's .value is always "" there — so that behavior is
  // covered by live browser verification, not a unit test.
});

describe("Shelf a11y", () => {
  test("offers provider settings only when the hosted endpoint is available", async () => {
    mocks.providerSettings.mockResolvedValue({ items: [] });
    render(<Shelf onOpen={() => {}} />);
    expect(
      await screen.findByRole("button", { name: /AI provider settings/i }),
    ).toBeTruthy();
  });

  test("each book is a real button (keyboard-openable, not a click-only div)", async () => {
    const user = userEvent.setup();
    mocks.books.mockResolvedValue([
      { book_id: "kar", title: "The Brothers Karamazov", author: "Dostoevsky" },
    ]);
    const onOpen = vi.fn();
    render(<Shelf onOpen={onOpen} />);
    const spine = await screen.findByRole("button", {
      name: /brothers karamazov/i,
    });
    spine.focus();
    await user.keyboard("{Enter}");
    expect(onOpen).toHaveBeenCalledWith("kar");
  });

  test("the import affordance is a labelled button", async () => {
    render(<Shelf onOpen={() => {}} />);
    expect(
      await screen.findByRole("button", { name: /add an epub/i }),
    ).toBeTruthy();
  });

  test("a spine's accessible name separates title from author (not 'KaramazovFyodor')", async () => {
    mocks.books.mockResolvedValue([
      {
        book_id: "kar",
        title: "The Brothers Karamazov",
        author: "Fyodor Dostoevsky",
      },
    ]);
    render(<Shelf onOpen={() => {}} />);
    const spine = await screen.findByRole("button", { name: /karamazov/i });
    expect(spine.getAttribute("aria-label")).toBe(
      "The Brothers Karamazov, Fyodor Dostoevsky",
    );
  });

  test("a book with no author reads as just the title", async () => {
    mocks.books.mockResolvedValue([
      { book_id: "x", title: "Untitled", author: null },
    ]);
    render(<Shelf onOpen={() => {}} />);
    const spine = await screen.findByRole("button", { name: "Untitled" });
    expect(spine.getAttribute("aria-label")).toBe("Untitled");
  });

  test("the import busy state is announced to assistive tech (role=status)", async () => {
    let release!: () => void;
    mocks.books.mockResolvedValue([]);
    mocks.importBook.mockReturnValue(
      new Promise<{ book_id: string }>((res) => {
        release = () => res({ book_id: "b" });
      }),
    );
    const { container } = render(<Shelf onOpen={() => {}} />);
    const input = container.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    const f = new File([new Uint8Array([1, 2, 3])], "b.epub", {
      type: "application/epub+zip",
    });
    fireEvent.change(input, { target: { files: [f] } });
    const status = await screen.findByRole("status");
    expect(status.textContent).toMatch(/adding a book/i);
    await act(async () => {
      release();
    });
    await waitFor(() =>
      expect(
        (
          screen.getByRole("button", {
            name: /add an epub/i,
          }) as HTMLButtonElement
        ).disabled,
      ).toBe(false),
    );
  });

  test("no axe violations", async () => {
    mocks.books.mockResolvedValue([
      { book_id: "kar", title: "The Brothers Karamazov", author: "Dostoevsky" },
    ]);
    const { container } = render(<Shelf onOpen={() => {}} />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /karamazov/i })).toBeTruthy(),
    );
    expect(await axeAA(container)).toHaveNoViolations();
  });
});

const library = [
  { book_id: "lantern", title: "The Lantern", author: "Ursula Le Guin" },
  { book_id: "atlas", title: "Atlas of Quiet Places", author: "Ada North" },
  { book_id: "sea", title: "A Sea of Stars", author: "Maya West" },
];
const ownerA = "05dbebd1-e9e8-4be8-8172-eb65ca8d2a67";
const ownerB = "ae7eb4ca-c15e-4b0e-9476-15e671a005b9";

describe("Library interactions", () => {
  test("shows loading before books arrive and offers a working retry after an error", async () => {
    const user = userEvent.setup();
    let reject!: (error: Error) => void;
    mocks.books
      .mockReturnValueOnce(
        new Promise((_, fail) => {
          reject = fail;
        }),
      )
      .mockResolvedValueOnce(library);
    render(<Shelf onOpen={() => {}} />);
    expect(
      screen.getByLabelText("Loading your library").getAttribute("aria-busy"),
    ).toBe("true");
    expect(screen.queryByText("Make yourself at home.")).toBeNull();
    await act(async () => {
      reject(new Error("Your library is temporarily unavailable."));
    });
    expect(screen.getByRole("alert").textContent).toContain(
      "temporarily unavailable",
    );
    expect(screen.queryByLabelText("Loading your library")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("button", {
        name: "The Lantern, Ursula Le Guin",
      }),
    ).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("searches titles and authors, explains no matches, and clears the filter", async () => {
    const user = userEvent.setup();
    mocks.books.mockResolvedValue(library);
    render(<Shelf onOpen={() => {}} />);
    await screen.findByRole("button", { name: "The Lantern, Ursula Le Guin" });
    const search = screen.getByRole("searchbox", {
      name: "Search your library",
    });
    await user.type(search, "LE GUIN");
    expect(
      screen.getByRole("button", { name: "The Lantern, Ursula Le Guin" }),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", {
        name: "Atlas of Quiet Places, Ada North",
      }),
    ).toBeNull();
    await user.clear(search);
    await user.type(search, "quiet places");
    expect(
      screen.getByRole("button", { name: "Atlas of Quiet Places, Ada North" }),
    ).toBeTruthy();
    await user.clear(search);
    await user.type(search, "missing book");
    expect(
      screen.getByRole("heading", { name: "No books found" }),
    ).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Show all books" }));
    expect((search as HTMLInputElement).value).toBe("");
    expect(
      screen.getByRole("button", { name: "A Sea of Stars, Maya West" }),
    ).toBeTruthy();
  });

  test("keeps books usable in list view and orders them by the chosen title or author", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    mocks.books.mockResolvedValue(library);
    render(<Shelf onOpen={onOpen} />);
    await screen.findByRole("button", { name: "The Lantern, Ursula Le Guin" });
    const list = screen.getByRole("button", { name: "List view" });
    await user.click(list);
    expect(list.getAttribute("aria-pressed")).toBe("true");
    expect(
      screen
        .getByRole("button", { name: "Grid view" })
        .getAttribute("aria-pressed"),
    ).toBe("false");
    const collection = screen.getByRole("region", { name: /Your books/i });
    const titles = () =>
      within(collection)
        .getAllByRole("button", {
          name: /^(The Lantern,|Atlas of Quiet Places,|A Sea of Stars,)/,
        })
        .map((button) => button.getAttribute("aria-label"));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Sort books" }),
      "title",
    );
    expect(titles()).toEqual([
      "A Sea of Stars, Maya West",
      "Atlas of Quiet Places, Ada North",
      "The Lantern, Ursula Le Guin",
    ]);
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Sort books" }),
      "author",
    );
    expect(titles()).toEqual([
      "Atlas of Quiet Places, Ada North",
      "A Sea of Stars, Maya West",
      "The Lantern, Ursula Le Guin",
    ]);
    screen
      .getByRole("button", { name: "Atlas of Quiet Places, Ada North" })
      .focus();
    await user.keyboard("{Enter}");
    expect(onOpen).toHaveBeenCalledWith("atlas");
  });

  test("rejects an invalid dropped file without starting an import", async () => {
    const { container } = render(<Shelf onOpen={() => {}} />);
    await screen.findByRole("heading", { name: "Make yourself at home." });
    const dataTransfer = {
      types: ["Files"],
      files: [new File(["pdf"], "book.pdf", { type: "application/pdf" })],
    };
    const main = container.firstElementChild!;
    fireEvent.dragEnter(main, { dataTransfer });
    expect(screen.getByText("Drop your next read here.")).toBeTruthy();
    fireEvent.drop(main, { dataTransfer });
    expect(screen.getByRole("alert").textContent).toContain(
      "Choose a DRM-free EPUB",
    );
    expect(screen.queryByText("Drop your next read here.")).toBeNull();
    expect(mocks.importBook).not.toHaveBeenCalled();
  });

  test("only offers complete downloads in the offline filter and shows save results visibly", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    mocks.books.mockResolvedValue(library);
    localStorage.setItem("litlet.offline.owner", ownerA);
    const stored = new Map<string, Response>([
      ["/api/books/lantern/epub", new Response("partial")],
      ["/api/books/sea/epub", new Response("epub")],
      ["/api/books/sea/manifest", new Response("{}")],
    ]);
    vi.stubGlobal("caches", {
      match: vi.fn(async (path: string, options: { cacheName: string }) =>
        options.cacheName === `litlet-reader-owner-${ownerA}`
          ? stored.get(path)
          : undefined,
      ),
      open: vi.fn(async () => ({
        put: async (path: string, response: Response) => {
          stored.set(path, response);
        },
        match: async (path: string) => stored.get(path),
      })),
    });
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response("{}", { status: 200 }));
    render(<Shelf onOpen={onOpen} />);
    await screen.findByRole("button", { name: "refresh offline copy" });
    await user.click(screen.getByRole("button", { name: "Available offline" }));
    expect(
      screen.getByRole("button", { name: "A Sea of Stars, Maya West" }),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "The Lantern, Ursula Le Guin" }),
    ).toBeNull();
    await user.click(screen.getByRole("button", { name: /All books/i }));
    const lantern = screen
      .getByRole("button", { name: "The Lantern, Ursula Le Guin" })
      .closest("article")!;
    fetch.mockRejectedValueOnce(new TypeError("Connection unavailable"));
    await user.click(
      within(lantern).getByRole("button", { name: "save for offline reading" }),
    );
    expect(
      await screen.findByText(
        "the book could not be saved for offline reading",
      ),
    ).toBeTruthy();
    expect(
      within(lantern).queryByRole("button", { name: "refresh offline copy" }),
    ).toBeNull();
    await user.click(
      within(lantern).getByRole("button", { name: "save for offline reading" }),
    );
    expect(
      await screen.findByText("The Lantern is ready for offline reading."),
    ).toBeTruthy();
    expect(
      within(lantern).getByRole("button", { name: "refresh offline copy" })
        .textContent,
    ).toContain("Saved offline");
    expect(onOpen).not.toHaveBeenCalled();
  });

  test("reads and writes recently opened books only for the current owner", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    mocks.books.mockResolvedValue(library);
    localStorage.setItem("litlet.offline.owner", ownerA);
    localStorage.setItem(
      `litlet.library-recent:${ownerA}`,
      JSON.stringify(["lantern"]),
    );
    localStorage.setItem(
      `litlet.library-recent:${ownerB}`,
      JSON.stringify(["sea"]),
    );
    const { rerender } = render(<Shelf onOpen={onOpen} />);
    await screen.findByRole("button", { name: "The Lantern, Ursula Le Guin" });
    await user.click(screen.getByRole("button", { name: "Recently opened" }));
    expect(
      screen.getByRole("button", { name: "The Lantern, Ursula Le Guin" }),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "A Sea of Stars, Maya West" }),
    ).toBeNull();
    localStorage.setItem("litlet.offline.owner", ownerB);
    rerender(<Shelf onOpen={onOpen} />);
    expect(
      screen.getByRole("button", { name: "A Sea of Stars, Maya West" }),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "The Lantern, Ursula Le Guin" }),
    ).toBeNull();
    await user.click(
      screen.getByRole("button", { name: "A Sea of Stars, Maya West" }),
    );
    expect(localStorage.getItem(`litlet.library-recent:${ownerA}`)).toBe(
      JSON.stringify(["lantern"]),
    );
    expect(localStorage.getItem(`litlet.library-recent:${ownerB}`)).toBe(
      JSON.stringify(["sea"]),
    );
    expect(onOpen).toHaveBeenCalledWith("sea");
  });
});
