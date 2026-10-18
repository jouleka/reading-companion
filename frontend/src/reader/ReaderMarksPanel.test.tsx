import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
import type { ReaderMark } from "../api";
import { ReaderMarks } from "./ReaderMarksPanel";

const mark: ReaderMark = {
  id: "m",
  kind: "highlight",
  anchor: { cfi: "epubcfi(/6/2)", atom: 1 },
  color: "yellow",
  selected_text: "A <dangerous> passage",
  body: null,
  label: null,
  version: 1,
  created_at: "now",
  updated_at: "now",
};
const props = {
  marks: [mark],
  selection: null,
  currentAnchor: { cfi: "epubcfi(/6/2)", atom: 1 },
  exportUrl: "/export",
  status: "saved",
  onAssist: vi.fn(),
  onHighlight: vi.fn(),
  onAnnotate: vi.fn(),
  onBookmark: vi.fn(),
  onNavigate: vi.fn(),
  onDelete: vi.fn(),
  onClearSelection: vi.fn(),
};

describe("ReaderMarks", () => {
  test("finds saved words within the selected notebook filter", () => {
    render(<ReaderMarks {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Notebook" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Search your notebook" }), { target: { value: "missing" } });
    expect(screen.queryByRole("button", { name: /highlight · chapter 1/i })).toBeNull();
    expect(screen.getByText("No saved passages match your search.")).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search your notebook" }), { target: { value: "DANGEROUS" } });
    expect(screen.getByRole("button", { name: /highlight · chapter 1/i })).toBeTruthy();
  });
  test("selection actions preserve text, save a color and note, and pass axe", async () => {
    const onHighlight = vi.fn();
    const onAnnotate = vi.fn();
    const onAssist = vi.fn();
    const { container } = render(
      <ReaderMarks
        {...props}
        selection={{ anchor: mark.anchor, text: "A <dangerous> passage" }}
        onHighlight={onHighlight}
        onAnnotate={onAnnotate}
        onAssist={onAssist}
      />,
    );
    expect(screen.queryByRole("button", { name: /^explain$/i })).toBeNull();
    expect(screen.queryByRole("textbox", { name: /add a note/i })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^highlight$/i }));
    fireEvent.click(screen.getByRole("button", { name: /yellow highlight/i }));
    expect(onHighlight).toHaveBeenCalledWith("yellow");
    fireEvent.click(screen.getByRole("button", { name: /^understand$/i }));
    expect(screen.queryByRole("button", { name: /yellow highlight/i })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^explain$/i }));
    expect(onAssist).toHaveBeenCalledWith("explain");
    fireEvent.click(screen.getByRole("button", { name: /^note$/i }));
    fireEvent.change(screen.getByLabelText(/add a note/i), {
      target: { value: "Remember this." },
    });
    fireEvent.click(screen.getByRole("button", { name: /save note/i }));
    expect(onAnnotate).toHaveBeenCalledWith("Remember this.");
    expect(container.querySelector("script")).toBeNull();
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("lists portable marks, navigates, exports, and restores focus on Escape", () => {
    const onNavigate = vi.fn();
    render(<ReaderMarks {...props} onNavigate={onNavigate} />);
    const opener = screen.getByRole("button", { name: /^notebook$/i });
    fireEvent.click(opener);
    expect(
      screen.getByRole("link", { name: /export marks/i }).getAttribute("href"),
    ).toBe("/export");
    fireEvent.click(
      screen.getByRole("button", { name: /highlight · chapter 1/i }),
    );
    expect(onNavigate).toHaveBeenCalledWith(mark);
    expect(
      screen.queryByRole("region", { name: /highlights, notes/i }),
    ).toBeNull();
    fireEvent.click(opener);
    fireEvent.keyDown(
      screen.getByRole("region", { name: /highlights, notes/i }),
      { key: "Escape" },
    );
    expect(opener).toBe(document.activeElement);
  });

  test("retains a note draft after a save failure and exposes its status beside the selection", () => {
    const selection = { anchor: mark.anchor, text: "A passage" };
    const { rerender } = render(
      <ReaderMarks {...props} status="" selection={selection} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /^note$/i }));
    const note = screen.getByRole("textbox", {
      name: /add a note/i,
    }) as HTMLTextAreaElement;
    fireEvent.change(note, { target: { value: "A note worth keeping" } });
    fireEvent.click(screen.getByRole("button", { name: /save note/i }));
    rerender(
      <ReaderMarks
        {...props}
        selection={selection}
        status="note could not be saved"
      />,
    );
    expect(note.value).toBe("A note worth keeping");
    expect(screen.getByRole("status").textContent).toBe(
      "note could not be saved",
    );
  });

  test("filters the notebook without changing or losing saved marks", () => {
    render(<ReaderMarks {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /^notebook$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^bookmarks/i }));
    expect(screen.queryByRole("button", { name: /highlight · chapter 1/i })).toBeNull();
    expect(screen.getByText("Nothing saved in this view yet.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^all/i }));
    expect(screen.getByRole("button", { name: /highlight · chapter 1/i })).toBeTruthy();
  });
});
