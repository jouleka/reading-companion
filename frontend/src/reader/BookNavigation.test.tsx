import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
import { ReaderNavigation } from "./BookNavigation";
import { mapReaderToc } from "./readerNavigation";

const atoms = [
  { ordinal: 1, href: "ch1.xhtml", title: "Chapter I", part_label: "Part I", char_len: 100 },
  { ordinal: 2, href: "ch2.xhtml", title: "", part_label: "", char_len: 100 },
];
const toc = mapReaderToc([
  { label: "raw part", subitems: [
    { label: "Chapter I", href: "ch1.xhtml" },
    { label: "Future spoiler", href: "ch2.xhtml" },
  ] },
], ["ch1.xhtml", "ch2.xhtml"], [0, 1], atoms);

const props = {
  toc,
  atoms,
  currentAtom: 0,
  canGoBack: true,
  canGoForward: false,
  searchableChapters: 1,
  onBack: vi.fn(),
  onForward: vi.fn(),
  onNavigate: vi.fn(),
  onSearch: vi.fn(async () => [{
    cfi: "epubcfi(/6/2)", atom: 0,
    excerpt: { pre: "A ", match: "lantern", post: " glowed." },
  }]),
  onSearchNavigate: vi.fn(),
};

describe("ReaderNavigation", () => {
  test("renders hierarchical spoiler-safe TOC and marks the current location", async () => {
    const { container } = render(<ReaderNavigation {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /^contents$/i }));
    const nav = screen.getByRole("navigation", { name: /table of contents/i });
    expect(within(nav).getByRole("button", { name: "Chapter I" }).getAttribute("aria-current"))
      .toBe("location");
    expect(nav.textContent).toContain("Part I");
    expect(nav.textContent).toContain("Chapter 2");
    expect(nav.textContent).not.toContain("Future spoiler");
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("searches read pages, renders escaped excerpts, and opens the exact CFI", async () => {
    const onSearchNavigate = vi.fn();
    render(<ReaderNavigation {...props} onSearchNavigate={onSearchNavigate} />);
    fireEvent.click(screen.getByRole("button", { name: /^contents$/i }));
    fireEvent.change(screen.getByRole("searchbox", { name: /find in pages/i }), {
      target: { value: "lantern" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^find$/i }));
    await waitFor(() => expect(props.onSearch).toHaveBeenCalledWith("lantern"));
    const result = screen.getByRole("button", { name: /lantern/i });
    expect(result.querySelector("mark")?.textContent).toBe("lantern");
    fireEvent.click(result);
    expect(onSearchNavigate).toHaveBeenCalledWith(expect.objectContaining({
      cfi: "epubcfi(/6/2)", atom: 0,
    }));
  });

  test("history controls and Escape focus restoration are keyboard accessible", () => {
    const onBack = vi.fn();
    render(<ReaderNavigation {...props} onBack={onBack} />);
    const opener = screen.getByRole("button", { name: /^contents$/i });
    fireEvent.click(opener);
    fireEvent.click(screen.getByRole("button", { name: /back/i }));
    expect(onBack).toHaveBeenCalled();
    fireEvent.keyDown(screen.getByRole("region", { name: /book navigation/i }), { key: "Escape" });
    expect(screen.queryByRole("region", { name: /book navigation/i })).toBeNull();
    expect(document.activeElement).toBe(opener);
  });
});
