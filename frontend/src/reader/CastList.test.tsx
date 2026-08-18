/** CastList (LIT-31): the roving-tabindex cast selector (absorbs LIT-28) — the accessible interactive
 * node navigation for the aria-hidden graph. Exactly one item is tabbable; arrows move focus among the
 * cast; Enter/click selects (re-centers the graph on that character). */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { GraphNode } from "../api";
import { axeAA } from "../test-a11y";
import { CastList } from "./CastList";

const chars: GraphNode[] = [
  { entity_id: 1, canonical_name: "Fyodor", type: "character", revealed_at: 1 },
  { entity_id: 2, canonical_name: "Mitya", type: "character", revealed_at: 1 },
  { entity_id: 3, canonical_name: "Ivan", type: "character", revealed_at: 2 },
];

describe("CastList", () => {
  test("roving tabindex: exactly one item tabbable, arrows move focus, Enter selects", () => {
    const onFocus = vi.fn();
    render(<CastList characters={chars} focusId={1} onFocus={onFocus} />);
    const items = screen.getAllByRole("option");
    expect(items.filter((el) => (el as HTMLElement).tabIndex === 0)).toHaveLength(1); // one roving stop
    (items[0] as HTMLElement).focus();
    fireEvent.keyDown(items[0], { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[1]); // arrow moved the roving focus
    fireEvent.keyDown(items[1], { key: "Enter" });
    expect(onFocus).toHaveBeenCalledWith(2); // Enter selects the focused item
  });

  test("the current focusId is marked selected; click also selects", () => {
    const onFocus = vi.fn();
    render(<CastList characters={chars} focusId={3} onFocus={onFocus} />);
    expect(screen.getByRole("option", { selected: true }).textContent).toContain("Ivan");
    fireEvent.click(screen.getByText("Fyodor"));
    expect(onFocus).toHaveBeenCalledWith(1);
  });

  test("no axe violations", async () => {
    const { container } = render(<CastList characters={chars} focusId={1} onFocus={() => {}} />);
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("focus is repaired onto a survivor when the focused option is removed (scrub-back shrink)", () => {
    // a11y review pass-1 HIGH (WCAG 2.4.3): retiming shrinks the cast; if the focused option unmounts,
    // the browser drops focus to <body> and a keyboard user is stranded behind the open dialog
    const { rerender } = render(<CastList characters={chars} focusId={1} onFocus={() => {}} />);
    const items = screen.getAllByRole("option");
    (items[2] as HTMLElement).focus(); // focus "Ivan" (revealed_at 2 — gone at T=1)
    fireEvent.keyDown(items[0], { key: "End" }); // move the roving index onto him too
    rerender(<CastList characters={chars.slice(0, 2)} focusId={1} onFocus={() => {}} />);
    const survivors = screen.getAllByRole("option");
    expect(document.activeElement).not.toBe(document.body); // never stranded
    expect(survivors).toContain(document.activeElement); // focus moved onto a surviving option
  });

  test("a shrink does NOT pull focus into the list when focus was already on <body> (not ours)", () => {
    // a11y review PASS-2 regression: the signature-only gate (shrank && orphaned) yanked focus into
    // the listbox whenever a shrink fired while focus happened to be on <body> for an unrelated
    // reason (WCAG 3.2.1). Repair must fire ONLY when THIS list held focus a moment ago.
    const { rerender } = render(<CastList characters={chars} focusId={1} onFocus={() => {}} />);
    (document.activeElement as HTMLElement | null)?.blur?.(); // focus on <body>, list never engaged
    expect(document.activeElement).toBe(document.body);
    rerender(<CastList characters={chars.slice(0, 2)} focusId={1} onFocus={() => {}} />);
    expect(document.activeElement).toBe(document.body); // NOT stolen into the listbox
  });

  test("a shrink does NOT steal focus parked outside the list (e.g. the scrubber)", () => {
    const { rerender } = render(
      <div>
        <input data-testid="slider" />
        <CastList characters={chars} focusId={1} onFocus={() => {}} />
      </div>,
    );
    screen.getByTestId("slider").focus(); // the user is driving the scrubber, not the cast
    rerender(
      <div>
        <input data-testid="slider" />
        <CastList characters={chars.slice(0, 2)} focusId={1} onFocus={() => {}} />
      </div>,
    );
    expect(document.activeElement).toBe(screen.getByTestId("slider")); // untouched
  });
});
