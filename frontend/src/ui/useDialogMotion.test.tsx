import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { useDialogMotion } from "./useDialogMotion";

function Dialog({ onClose }: { onClose: () => void }) {
  const { closing, requestClose } = useDialogMotion(onClose);
  return (
    <div
      role="dialog"
      aria-label="Test dialog"
      data-closing={closing || undefined}
    >
      <button onClick={requestClose}>Close</button>
    </div>
  );
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useDialogMotion", () => {
  test("keeps the dialog and its focused control mounted during a single exit", () => {
    vi.useFakeTimers();
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: false }));
    const onClose = vi.fn();
    render(<Dialog onClose={onClose} />);
    const close = screen.getByRole("button", { name: "Close" });
    close.focus();
    fireEvent.click(close);
    fireEvent.click(close);
    expect(screen.getByRole("dialog").getAttribute("data-closing")).toBe(
      "true",
    );
    expect(document.activeElement).toBe(close);
    act(() => vi.advanceTimersByTime(159));
    expect(onClose).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("uses the latest close callback after the exit finishes", () => {
    vi.useFakeTimers();
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: false }));
    const first = vi.fn();
    const latest = vi.fn();
    const { rerender } = render(<Dialog onClose={first} />);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    rerender(<Dialog onClose={latest} />);
    act(() => vi.advanceTimersByTime(160));
    expect(first).not.toHaveBeenCalled();
    expect(latest).toHaveBeenCalledTimes(1);
  });

  test("closes immediately when reduced motion is preferred", () => {
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: true }));
    const onClose = vi.fn();
    render(<Dialog onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("cancels a pending close when navigation unmounts the dialog", () => {
    vi.useFakeTimers();
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: false }));
    const onClose = vi.fn();
    const { unmount } = render(<Dialog onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    unmount();
    act(() => vi.advanceTimersByTime(160));
    expect(onClose).not.toHaveBeenCalled();
  });
});
