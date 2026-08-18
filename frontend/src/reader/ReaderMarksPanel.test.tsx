import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
import type { ReaderMark } from "../api";
import { ReaderMarks } from "./ReaderMarksPanel";

const mark: ReaderMark = {
  id: "m", kind: "highlight", anchor: { cfi: "epubcfi(/6/2)", atom: 1 },
  color: "yellow", selected_text: "A <dangerous> passage", body: null, label: null,
  version: 1, created_at: "now", updated_at: "now",
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
    fireEvent.click(screen.getByRole("button", { name: /yellow highlight/i }));
    expect(onHighlight).toHaveBeenCalledWith("yellow");
    fireEvent.click(screen.getByRole("button", { name: /^explain$/i }));
    expect(onAssist).toHaveBeenCalledWith("explain");
    fireEvent.change(screen.getByLabelText(/add a note/i), { target: { value: "Remember this." } });
    fireEvent.click(screen.getByRole("button", { name: /save note/i }));
    expect(onAnnotate).toHaveBeenCalledWith("Remember this.");
    expect(container.querySelector("script")).toBeNull();
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("lists portable marks, navigates, exports, and restores focus on Escape", () => {
    const onNavigate = vi.fn();
    render(<ReaderMarks {...props} onNavigate={onNavigate} />);
    const opener = screen.getByRole("button", { name: /^notes$/i });
    fireEvent.click(opener);
    fireEvent.click(screen.getByRole("button", { name: /highlight · chapter 1/i }));
    expect(onNavigate).toHaveBeenCalledWith(mark);
    expect(screen.getByRole("link", { name: /export marks/i }).getAttribute("href")).toBe("/export");
    fireEvent.keyDown(screen.getByRole("region", { name: /highlights, notes/i }), { key: "Escape" });
    expect(opener).toBe(document.activeElement);
  });
});
