/** The jump-confirmation strip is an alert dialog: it interrupts to ask a spoiler-relevant question
 * ("this marks everything before it as read"), so keyboard users must land in it and be able to
 * dismiss it without a mouse. */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
import { JumpNote } from "./JumpNote";

describe("JumpNote", () => {
  test("is an alertdialog naming the target chapter", () => {
    render(<JumpNote chapter={91} onFollow={() => {}} onStay={() => {}} />);
    const dlg = screen.getByRole("alertdialog");
    expect(dlg.textContent).toMatch(/chapter 91/);
  });

  test("moves focus onto a control when it appears (keyboard users are not stranded)", () => {
    render(<JumpNote chapter={5} onFollow={() => {}} onStay={() => {}} />);
    const active = document.activeElement as HTMLElement;
    expect(active.tagName).toBe("BUTTON");
    expect(active.closest('[role="alertdialog"]')).not.toBeNull();
  });

  test("Escape dismisses via onStay (the safe default)", () => {
    const onStay = vi.fn();
    render(<JumpNote chapter={5} onFollow={() => {}} onStay={onStay} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onStay).toHaveBeenCalledTimes(1);
  });

  test("follow and stay buttons fire their handlers", () => {
    const onFollow = vi.fn();
    const onStay = vi.fn();
    render(<JumpNote chapter={5} onFollow={onFollow} onStay={onStay} />);
    fireEvent.click(screen.getByRole("button", { name: /follow/i }));
    fireEvent.click(screen.getByRole("button", { name: /stay/i }));
    expect(onFollow).toHaveBeenCalledTimes(1);
    expect(onStay).toHaveBeenCalledTimes(1);
  });

  test("traps Tab within its own controls (cannot reach the reader behind it)", () => {
    render(<JumpNote chapter={5} onFollow={() => {}} onStay={() => {}} />);
    const follow = screen.getByRole("button", { name: /follow/i });
    const stay = screen.getByRole("button", { name: /stay/i });
    expect(document.activeElement).toBe(stay); // opens on the safe choice
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(follow); // wraps to the other control, never escapes
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(stay); // shift-tab wraps back
  });

  test("restores focus to the trigger when it closes (WCAG 2.4.3)", () => {
    const trigger = document.createElement("button");
    document.body.appendChild(trigger);
    trigger.focus();
    expect(document.activeElement).toBe(trigger);
    const { unmount } = render(<JumpNote chapter={5} onFollow={() => {}} onStay={() => {}} />);
    expect(document.activeElement).not.toBe(trigger); // focus moved into the dialog
    unmount();
    expect(document.activeElement).toBe(trigger); // and returned on close
    trigger.remove();
  });

  test("has no axe violations", async () => {
    const { container } = render(<JumpNote chapter={5} onFollow={() => {}} onStay={() => {}} />);
    expect(await axeAA(container)).toHaveNoViolations();
  });
});
