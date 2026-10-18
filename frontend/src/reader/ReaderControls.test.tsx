import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
import { ReaderControls } from "./ReaderControls";
import { DEFAULT_READER_PREFERENCES } from "./readerPreferences";
import { ReaderTTS } from "./ReaderTTS";
import { POPOVER_EXIT_MS } from "../ui/useReaderPopover";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("ReaderControls", () => {
  test("prioritizes theme and size, and reveals native layout presets on request", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { container } = render(
      <ReaderControls
        preferences={DEFAULT_READER_PREFERENCES}
        onChange={onChange}
        status="saved"
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: /reading appearance/i }),
    );
    expect(screen.getAllByRole("group")).toHaveLength(2);
    expect(screen.queryByRole("radio", { name: "Publisher" })).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: /^large$/i }));
    expect(onChange).toHaveBeenCalledWith({
      ...DEFAULT_READER_PREFERENCES,
      font_size: "large",
    });
    await user.click(screen.getByText("Customize"));
    expect(screen.getAllByRole("group")).toHaveLength(6);
    expect(screen.getByRole("radio", { name: "Publisher" })).toBeTruthy();
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("Escape closes the controls and restores focus to the opener", () => {
    render(
      <ReaderControls
        preferences={DEFAULT_READER_PREFERENCES}
        onChange={() => {}}
        status="saved"
      />,
    );
    const opener = screen.getByRole("button", { name: /reading appearance/i });
    fireEvent.click(opener);
    fireEvent.keyDown(
      screen.getByRole("region", { name: /reading appearance/i }),
      { key: "Escape" },
    );
    expect(
      screen.queryByRole("region", { name: /reading appearance/i }),
    ).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  test("toolbar panels share focus and dismissal behavior without overlapping", async () => {
    const user = userEvent.setup();
    render(
      <>
        <ReaderControls
          preferences={DEFAULT_READER_PREFERENCES}
          onChange={() => {}}
          status="saved"
        />
        <ReaderTTS getView={() => null} resetKey="book" />
        <button type="button">Outside action</button>
      </>,
    );
    await user.click(
      screen.getByRole("button", { name: "reading appearance" }),
    );
    expect(document.activeElement).toBe(
      screen.getByRole("region", { name: "Reading appearance" }),
    );
    await user.click(screen.getByRole("button", { name: "read aloud" }));
    expect(
      screen.queryByRole("region", { name: "Reading appearance" }),
    ).toBeNull();
    expect(document.activeElement).toBe(
      screen.getByRole("region", { name: "Read aloud controls" }),
    );
    const outside = screen.getByRole("button", { name: "Outside action" });
    await user.click(outside);
    expect(
      screen.queryByRole("region", { name: "Read aloud controls" }),
    ).toBeNull();
    expect(document.activeElement).toBe(outside);
  });

  test("closing motion is noninteractive, can be reopened, and yields immediately to another panel", () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({ matches: false })),
    );
    render(
      <>
        <ReaderControls
          preferences={DEFAULT_READER_PREFERENCES}
          onChange={() => {}}
          status="saved"
        />
        <ReaderTTS getView={() => null} resetKey="book" />
      </>,
    );
    const opener = screen.getByRole("button", { name: "reading appearance" });
    fireEvent.click(opener);
    const panel = screen.getByRole("region", { name: "Reading appearance" });
    fireEvent.keyDown(document, { key: "Escape" });
    expect(panel.isConnected).toBe(true);
    expect(panel.getAttribute("data-closing")).toBe("true");
    expect(panel.inert).toBe(true);
    expect(
      screen.queryByRole("region", { name: "Reading appearance" }),
    ).toBeNull();
    expect(document.activeElement).toBe(opener);
    fireEvent.click(opener);
    act(() => {
      vi.advanceTimersByTime(POPOVER_EXIT_MS + 1);
    });
    expect(screen.getByRole("region", { name: "Reading appearance" })).toBe(
      panel,
    );
    expect(panel.inert).toBe(false);
    fireEvent.keyDown(document, { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: "read aloud" }));
    expect(panel.isConnected).toBe(false);
    expect(
      screen.getByRole("region", { name: "Read aloud controls" }),
    ).toBeTruthy();
  });

  test("reduced motion removes a closing panel immediately", () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({ matches: true })),
    );
    const { container } = render(
      <ReaderControls
        preferences={DEFAULT_READER_PREFERENCES}
        onChange={() => {}}
        status="saved"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "reading appearance" }));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(container.querySelector(".appearance-panel")).toBeNull();
  });
});
