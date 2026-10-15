/** The revealed_at scrubber (LIT-15): a slider that walks the memory back through time. A native
 * range input — role=slider + arrow-operable + aria-valuemin/max/now for free — styled as a ribbon. */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { Scrubber } from "./Scrubber";

describe("Scrubber", () => {
  test("is a slider bounded 1..high-water at the current value", () => {
    render(<Scrubber max={5} value={3} onChange={() => {}} />);
    const s = screen.getByRole("slider") as HTMLInputElement;
    expect(s.min).toBe("1");
    expect(s.max).toBe("5");
    expect(s.value).toBe("3");
    expect(s.getAttribute("aria-valuetext")).toMatch(/chapter 3/i);
  });

  test("reports keyboard and assistive changes without requiring a pointer", () => {
    const onChange = vi.fn();
    render(<Scrubber max={5} value={3} onChange={onChange} />);
    fireEvent.change(screen.getByRole("slider"), { target: { value: "2" } });
    expect(onChange).toHaveBeenCalledWith(2);
  });

  test("previews a pointer drag and commits only its final chapter", () => {
    const onChange = vi.fn();
    render(<Scrubber max={20} value={3} onChange={onChange} />);
    const slider = screen.getByRole("slider");
    fireEvent.pointerDown(slider, { pointerId: 1 });
    fireEvent.change(slider, { target: { value: "6" } });
    fireEvent.change(slider, { target: { value: "12" } });
    expect(slider.getAttribute("aria-valuetext")).toBe("chapter 12");
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.pointerUp(slider, { pointerId: 1 });
    fireEvent.lostPointerCapture(slider, { pointerId: 1 });
    expect(onChange).toHaveBeenCalledExactlyOnceWith(12);
  });

  test("cancelling a drag restores the selected chapter without re-querying", () => {
    const onChange = vi.fn();
    render(<Scrubber max={20} value={3} onChange={onChange} />);
    const slider = screen.getByRole("slider") as HTMLInputElement;
    fireEvent.pointerDown(slider, { pointerId: 1 });
    fireEvent.change(slider, { target: { value: "12" } });
    fireEvent.pointerCancel(slider, { pointerId: 1 });
    expect(slider.value).toBe("3");
    expect(onChange).not.toHaveBeenCalled();
  });

  test("does not re-query when a drag returns to the selected chapter", () => {
    const onChange = vi.fn();
    render(<Scrubber max={20} value={3} onChange={onChange} />);
    const slider = screen.getByRole("slider");
    fireEvent.pointerDown(slider, { pointerId: 1 });
    fireEvent.change(slider, { target: { value: "12" } });
    fireEvent.change(slider, { target: { value: "3" } });
    fireEvent.pointerUp(slider, { pointerId: 1 });
    expect(onChange).not.toHaveBeenCalled();
  });

  test("follows a chapter selected elsewhere", () => {
    const { rerender } = render(
      <Scrubber max={20} value={3} onChange={() => {}} />,
    );
    rerender(<Scrubber max={20} value={8} onChange={() => {}} />);
    expect((screen.getByRole("slider") as HTMLInputElement).value).toBe("8");
  });

  test("shows a legible 'as of chapter' label", () => {
    render(<Scrubber max={5} value={4} onChange={() => {}} />);
    expect(screen.getByText(/as of chapter IV/)).toBeTruthy();
  });
});
