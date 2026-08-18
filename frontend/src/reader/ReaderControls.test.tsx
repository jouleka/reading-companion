import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
import { ReaderControls } from "./ReaderControls";
import { DEFAULT_READER_PREFERENCES } from "./readerPreferences";

describe("ReaderControls", () => {
  test("exposes every preset as native keyboard-operable radio groups", async () => {
    const onChange = vi.fn();
    const { container } = render(
      <ReaderControls preferences={DEFAULT_READER_PREFERENCES} onChange={onChange} status="saved" />,
    );
    fireEvent.click(screen.getByRole("button", { name: /reading appearance/i }));
    expect(screen.getAllByRole("group")).toHaveLength(6);
    fireEvent.click(screen.getByRole("radio", { name: /^large$/i }));
    expect(onChange).toHaveBeenCalledWith({
      ...DEFAULT_READER_PREFERENCES,
      font_size: "large",
    });
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("Escape closes the controls and restores focus to the opener", () => {
    render(
      <ReaderControls preferences={DEFAULT_READER_PREFERENCES} onChange={() => {}} status="saved" />,
    );
    const opener = screen.getByRole("button", { name: /reading appearance/i });
    fireEvent.click(opener);
    fireEvent.keyDown(screen.getByRole("region", { name: /reading appearance/i }), { key: "Escape" });
    expect(screen.queryByRole("region", { name: /reading appearance/i })).toBeNull();
    expect(document.activeElement).toBe(opener);
  });
});
