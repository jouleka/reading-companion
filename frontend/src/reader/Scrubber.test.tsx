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

  test("reports the new chapter on change (arrow keys / drag both fire this)", () => {
    const onChange = vi.fn();
    render(<Scrubber max={5} value={3} onChange={onChange} />);
    fireEvent.change(screen.getByRole("slider"), { target: { value: "2" } });
    expect(onChange).toHaveBeenCalledWith(2);
  });

  test("shows a legible 'as of chapter' label", () => {
    render(<Scrubber max={5} value={4} onChange={() => {}} />);
    expect(screen.getByText(/as of chapter IV/)).toBeTruthy();
  });
});
