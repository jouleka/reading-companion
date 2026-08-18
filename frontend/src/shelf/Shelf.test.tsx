/** Shelf import-failure behavior: the error strip shows the server's human message (not raw
 * status + JSON), and the file input resets so re-selecting the same file retries. */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({
  books: vi.fn(),
  importBook: vi.fn(),
  providerSettings: vi.fn(),
}));
vi.mock("../api", () => ({ api: {
  books: mocks.books,
  importBook: mocks.importBook,
  providerSettings: mocks.providerSettings,
} }));

import { Shelf } from "./Shelf";

beforeEach(() => {
  mocks.books.mockReset().mockResolvedValue([]);
  mocks.importBook.mockReset();
  mocks.providerSettings.mockReset().mockRejectedValue(new Error("local mode"));
});

function selectFile(input: HTMLInputElement) {
  const f = new File([new Uint8Array([1, 2, 3])], "junk.epub", { type: "application/epub+zip" });
  fireEvent.change(input, { target: { files: [f] } });
}

describe("Shelf import failure", () => {
  test("shows the server's message, not raw status + JSON", async () => {
    mocks.importBook.mockRejectedValue(
      new Error('not a readable EPUB: BadZipFile'),
    );
    const { container } = render(<Shelf onOpen={() => {}} />);
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
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
    expect(await screen.findByRole("button", { name: /AI provider settings/i })).toBeTruthy();
  });

  test("each book is a real button (keyboard-openable, not a click-only div)", async () => {
    mocks.books.mockResolvedValue([
      { book_id: "kar", title: "The Brothers Karamazov", author: "Dostoevsky" },
    ]);
    const onOpen = vi.fn();
    render(<Shelf onOpen={onOpen} />);
    const spine = await screen.findByRole("button", { name: /brothers karamazov/i });
    fireEvent.keyDown(spine, { key: "Enter" });
    spine.click();
    expect(onOpen).toHaveBeenCalledWith("kar");
  });

  test("the import affordance is a labelled button", async () => {
    render(<Shelf onOpen={() => {}} />);
    expect(await screen.findByRole("button", { name: /add an epub/i })).toBeTruthy();
  });

  test("a spine's accessible name separates title from author (not 'KaramazovFyodor')", async () => {
    mocks.books.mockResolvedValue([
      { book_id: "kar", title: "The Brothers Karamazov", author: "Fyodor Dostoevsky" },
    ]);
    render(<Shelf onOpen={() => {}} />);
    const spine = await screen.findByRole("button", { name: /karamazov/i });
    expect(spine.getAttribute("aria-label")).toBe("The Brothers Karamazov, Fyodor Dostoevsky");
  });

  test("a book with no author reads as just the title", async () => {
    mocks.books.mockResolvedValue([{ book_id: "x", title: "Untitled", author: null }]);
    render(<Shelf onOpen={() => {}} />);
    const spine = await screen.findByRole("button", { name: "Untitled" });
    expect(spine.getAttribute("aria-label")).toBe("Untitled");
  });

  test("the import busy state is announced to assistive tech (role=status)", async () => {
    let release!: () => void;
    mocks.books.mockResolvedValue([]);
    mocks.importBook.mockReturnValue(new Promise<{ book_id: string }>((res) => {
      release = () => res({ book_id: "b" });
    }));
    const { container } = render(<Shelf onOpen={() => {}} />);
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const f = new File([new Uint8Array([1, 2, 3])], "b.epub", { type: "application/epub+zip" });
    fireEvent.change(input, { target: { files: [f] } });
    const status = await screen.findByRole("status");
    expect(status.textContent).toMatch(/adding a book/i);
    release();
  });

  test("no axe violations", async () => {
    mocks.books.mockResolvedValue([
      { book_id: "kar", title: "The Brothers Karamazov", author: "Dostoevsky" },
    ]);
    const { container } = render(<Shelf onOpen={() => {}} />);
    await waitFor(() => expect(screen.getByRole("button", { name: /karamazov/i })).toBeTruthy());
    expect(await axeAA(container)).toHaveNoViolations();
  });
});
