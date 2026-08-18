/** LIT-30 the live name card: click a name -> a bookmark-clamped popover with who they are + their
 * ties. Renders ONLY the server-clamped /character payload (never client-computed or future data); its
 * tie chips walk the graph. It is a labelled dialog with Escape-to-close + focus restore. */
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({ character: vi.fn() }));
vi.mock("../api", () => ({ api: { character: mocks.character } }));

import { NameCard } from "./NameCard";

const card = {
  as_of_chapter: 5,
  entity_id: 5,
  name: "Fyodor Pavlovitch Karamazov",
  type: "character",
  aliases: ["Fyodor", "the buffoon"],
  first_seen: 1,
  status: null,
  ties: [
    { entity_id: 8, name: "Alexey", rel_type: "family", label: "father of", direction: "out" },
    { entity_id: 9, name: "Grigory", rel_type: "servant", label: "served by", direction: "in" },
  ],
};

beforeEach(() => mocks.character.mockReset());
afterEach(() => {
  vi.restoreAllMocks();
});

describe("NameCard", () => {
  test("fetches by (book, entity, bookmark) and renders identity + ties", async () => {
    mocks.character.mockResolvedValue(card);
    render(<NameCard bookId="b" entityId={5} bookmark={5} onClose={() => {}} onNavigate={() => {}} />);
    await screen.findByText(/Fyodor Pavlovitch Karamazov/);
    expect(screen.getByText(/buffoon/)).toBeTruthy();          // aliases
    expect(screen.getByText(/first seen/i)).toBeTruthy();      // first_seen
    expect(screen.getByText(/father of/i)).toBeTruthy();       // a tie label
    expect(screen.getByRole("button", { name: "Alexey" })).toBeTruthy();
    expect(mocks.character).toHaveBeenCalledWith("b", 5, 5);
  });

  test("clicking a tie navigates to that character (walk the graph)", async () => {
    mocks.character.mockResolvedValue(card);
    const onNavigate = vi.fn();
    render(<NameCard bookId="b" entityId={5} bookmark={5} onClose={() => {}} onNavigate={onNavigate} />);
    await screen.findByRole("button", { name: "Alexey" });
    fireEvent.click(screen.getByRole("button", { name: "Alexey" }));
    expect(onNavigate).toHaveBeenCalledWith(8);
  });

  test("is a dialog labelled by the character name; Escape closes it", async () => {
    mocks.character.mockResolvedValue(card);
    const onClose = vi.fn();
    render(<NameCard bookId="b" entityId={5} bookmark={5} onClose={onClose} onNavigate={() => {}} />);
    await screen.findByText(/Fyodor Pavlovitch Karamazov/);
    expect(screen.getByRole("dialog", { name: /Fyodor Pavlovitch Karamazov/ })).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("Tab moves through every tie instead of jumping from close to the last tie", async () => {
    mocks.character.mockResolvedValue(card);
    const user = userEvent.setup();
    render(<NameCard bookId="b" entityId={5} bookmark={5} onClose={() => {}} onNavigate={() => {}} />);
    await screen.findByRole("button", { name: "Alexey" });
    const close = screen.getByRole("button", { name: /close/i });
    close.focus();
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Alexey" }));
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Grigory" }));
  });

  test("shows a graceful message when the character is not found (404)", async () => {
    mocks.character.mockRejectedValue(new Error("unknown character"));
    render(<NameCard bookId="b" entityId={999} bookmark={5} onClose={() => {}} onNavigate={() => {}} />);
    expect(await screen.findByText(/could not be found|not found/i)).toBeTruthy();
  });

  test("restores focus to the trigger chip when the card closes (WCAG 2.4.3)", async () => {
    mocks.character.mockResolvedValue(card);
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button id="chip" onClick={() => setOpen(true)}>Fyodor</button>
          {open && (
            <NameCard bookId="b" entityId={5} bookmark={5} onClose={() => setOpen(false)} onNavigate={() => {}} />
          )}
        </>
      );
    }
    render(<Harness />);
    const chip = document.getElementById("chip")!;
    chip.focus();
    fireEvent.click(chip); // opens the card WHILE the chip holds focus (as a real click does)
    await screen.findByText(/Fyodor Pavlovitch Karamazov/);
    fireEvent.keyDown(document, { key: "Escape" }); // closes it
    expect(document.activeElement).toBe(chip); // focus must return to the chip, not fall to <body>
  });

  test("no axe violations", async () => {
    mocks.character.mockResolvedValue(card);
    const { container } = render(
      <NameCard bookId="b" entityId={5} bookmark={5} onClose={() => {}} onNavigate={() => {}} />,
    );
    await screen.findByText(/Fyodor Pavlovitch Karamazov/);
    expect(await axeAA(container)).toHaveNoViolations();
  });
});
