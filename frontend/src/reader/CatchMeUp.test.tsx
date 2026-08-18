/** The catch-me-up HERO (LIT-14): a full "the story so far" view over the clamped /catch-me-up route.
 * It is a dialog — focus moves in, Escape closes, focus restores (the LIT-16 pattern) — and it renders
 * ONLY the server-clamped recap/cast/threads (never client-computed or future data). */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({ catchMeUp: vi.fn(), character: vi.fn() }));
vi.mock("../api", () => ({ api: { catchMeUp: mocks.catchMeUp, character: mocks.character } }));

import { CatchMeUp } from "./CatchMeUp";

const recap = {
  recap: "Fyodor married twice.\n\nHis sons grew up apart.",
  as_of_chapter: 4,
  cast_size: 9,
  open_threads: 5,
  cached: false,
};

beforeEach(() => mocks.catchMeUp.mockReset());
afterEach(() => {
  vi.restoreAllMocks();
});

describe("CatchMeUp hero", () => {
  test("renders the recap prose, cast size and open threads once loaded", async () => {
    mocks.catchMeUp.mockResolvedValue(recap);
    render(<CatchMeUp bookId="b" bookmark={4} totalAtoms={96} onClose={() => {}} />);
    await screen.findByText(/Fyodor married twice/);
    expect(screen.getByText(/His sons grew up apart/)).toBeTruthy();
    expect(screen.getByText("9")).toBeTruthy(); // cast
    expect(screen.getByText("5")).toBeTruthy(); // threads
    expect(mocks.catchMeUp).toHaveBeenCalledWith("b", 4);
  });

  test("shows a nothing-read-yet state when the frontier is 0", async () => {
    mocks.catchMeUp.mockResolvedValue({ recap: null, as_of_chapter: 0, cast_size: 0, open_threads: 0, cached: false });
    render(<CatchMeUp bookId="b" bookmark={0} totalAtoms={96} onClose={() => {}} />);
    expect(await screen.findByText(/finish a chapter/i)).toBeTruthy();
    expect(screen.getByText(/nothing read yet/i)).toBeTruthy();
  });

  test("shows a graceful message when the recap fails the gate (502)", async () => {
    mocks.catchMeUp.mockRejectedValue(new Error("recap generation failed the spoiler gate"));
    render(<CatchMeUp bookId="b" bookmark={4} totalAtoms={96} onClose={() => {}} />);
    expect(await screen.findByText(/spoiler gate|try again/i)).toBeTruthy();
  });

  test("distinguishes rejected provider credentials from a spoiler-gate rejection", async () => {
    mocks.catchMeUp.mockRejectedValue(Object.assign(new Error("provider unavailable"), {
      code: "provider_authentication_failed",
      status: 503,
    }));
    render(<CatchMeUp bookId="b" bookmark={4} totalAtoms={96} onClose={() => {}} />);
    expect(await screen.findByText(/AI companion is offline.*credentials were rejected/i)).toBeTruthy();
    expect(screen.queryByText(/spoiler gate/i)).toBeNull();
  });

  test("is a labelled dialog; Escape closes it", async () => {
    mocks.catchMeUp.mockResolvedValue(recap);
    const onClose = vi.fn();
    render(<CatchMeUp bookId="b" bookmark={4} totalAtoms={96} onClose={onClose} />);
    await screen.findByText(/Fyodor married twice/);
    expect(screen.getByRole("dialog", { name: /story so far|catch me up/i })).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("moves focus into the dialog on open", async () => {
    mocks.catchMeUp.mockResolvedValue(recap);
    render(<CatchMeUp bookId="b" bookmark={4} totalAtoms={96} onClose={() => {}} />);
    await screen.findByText(/Fyodor married twice/);
    const active = document.activeElement as HTMLElement;
    expect(active.closest('[role="dialog"]')).not.toBeNull();
  });

  test("traps Tab within the dialog (focus cannot escape to the reader behind the scrim)", async () => {
    mocks.catchMeUp.mockResolvedValue(recap);
    const { container } = render(
      <>
        <button id="behind">a reader control behind the hero</button>
        <CatchMeUp bookId="b" bookmark={4} totalAtoms={96} onClose={() => {}} />
      </>,
    );
    await screen.findByText(/Fyodor married twice/);
    const dialog = container.querySelector('[role="dialog"]')!;
    // simulate focus escaping to a control behind the opaque scrim, then a Tab: the trap pulls it back
    (container.querySelector("#behind") as HTMLElement).focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(document.activeElement!.id).not.toBe("behind");
  });

  test("no axe violations", async () => {
    mocks.catchMeUp.mockResolvedValue(recap);
    const { container } = render(<CatchMeUp bookId="b" bookmark={4} totalAtoms={96} onClose={() => {}} />);
    await screen.findByText(/Fyodor married twice/);
    expect(await axeAA(container)).toHaveNoViolations();
  });

  const cardOf = (over = {}) => ({
    as_of_chapter: 4, entity_id: 5, name: "Fyodor", type: "character",
    aliases: [], first_seen: 1, status: null, ties: [], ...over,
  });

  test("renders cast names as clickable buttons carrying entity_id (LIT-30)", async () => {
    mocks.catchMeUp.mockResolvedValue({
      ...recap,
      recap: "Fyodor raised his sons. Alyosha entered the monastery.",
      now: "Alyosha has entered the monastery.",
      cast: [{ name: "Fyodor", entity_id: 5 }, { name: "Alyosha", entity_id: 8 }],
    });
    const { container } = render(<CatchMeUp bookId="b" bookmark={4} totalAtoms={96} onClose={() => {}} />);
    await screen.findByRole("button", { name: "Fyodor" });
    const chips = Array.from(container.querySelectorAll(".name-chip"));
    expect(chips.map((c) => c.textContent)).toEqual(["Fyodor", "Alyosha"]);
    expect(chips.every((c) => c.tagName.toLowerCase() === "button")).toBe(true); // live now, not inert
    expect(chips.map((c) => c.getAttribute("data-entity-id"))).toEqual(["5", "8"]);
    // a non-cast word is never wrapped
    expect(chips.map((c) => c.textContent)).not.toContain("monastery");
  });

  test("clicking a name opens its card, fetched at the current bookmark (LIT-30)", async () => {
    mocks.catchMeUp.mockResolvedValue({
      ...recap,
      recap: "Fyodor raised Alyosha.",
      cast: [{ name: "Fyodor", entity_id: 5 }, { name: "Alyosha", entity_id: 8 }],
    });
    mocks.character.mockResolvedValue(cardOf({ entity_id: 5, name: "Fyodor Pavlovitch" }));
    render(<CatchMeUp bookId="b" bookmark={4} totalAtoms={96} onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "Fyodor" }));
    await screen.findByRole("dialog", { name: /Fyodor Pavlovitch/ });
    expect(mocks.character).toHaveBeenCalledWith("b", 5, 4); // clamped to the hero's bookmark
  });

  test("marks the hero content inert while a name card is open (nested-modal tightening)", async () => {
    mocks.catchMeUp.mockResolvedValue({
      ...recap, recap: "Fyodor raised Alyosha.", cast: [{ name: "Fyodor", entity_id: 5 }],
    });
    mocks.character.mockResolvedValue(cardOf({ entity_id: 5, name: "Fyodor" }));
    const { container } = render(<CatchMeUp bookId="b" bookmark={4} totalAtoms={96} onClose={() => {}} />);
    const article = container.querySelector(".hero-page") as HTMLElement;
    expect(article.inert).toBeFalsy();
    fireEvent.click(await screen.findByRole("button", { name: "Fyodor" }));
    await screen.findByRole("dialog", { name: /Fyodor/ });
    expect(article.inert).toBe(true); // the hero is inert while the card is the active modal
    // and un-inerted on close (a layout effect, so it clears BEFORE the card's focus-restore runs —
    // jsdom can't see the inert-vs-focus timing; that half is live-verified)
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(article.inert).toBe(false));
  });

  test("the recap still round-trips its full text with names wrapped (no dropped words)", async () => {
    mocks.catchMeUp.mockResolvedValue({
      ...recap, recap: "Fyodor raised Alyosha well.",
      cast: [{ name: "Fyodor", entity_id: 5 }, { name: "Alyosha", entity_id: 8 }],
    });
    const { container } = render(<CatchMeUp bookId="b" bookmark={4} totalAtoms={96} onClose={() => {}} />);
    await screen.findByRole("button", { name: "Fyodor" });
    expect(container.querySelector(".hero-recap")!.textContent).toBe("Fyodor raised Alyosha well.");
  });

  test("no axe violations with clickable names present", async () => {
    mocks.catchMeUp.mockResolvedValue({
      ...recap, recap: "Fyodor raised Alyosha.",
      cast: [{ name: "Fyodor", entity_id: 5 }, { name: "Alyosha", entity_id: 8 }],
    });
    const { container } = render(<CatchMeUp bookId="b" bookmark={4} totalAtoms={96} onClose={() => {}} />);
    await screen.findByRole("button", { name: "Fyodor" });
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("welcome-back: framing shows visually AND in the dialog's accessible name (announced on open)", async () => {
    mocks.catchMeUp.mockResolvedValue(recap);
    render(
      <CatchMeUp bookId="b" bookmark={4} totalAtoms={96} welcomeBackChapter={4} onClose={() => {}} />,
    );
    await screen.findByText(/Fyodor married twice/);
    // visible banner for sighted readers
    const banner = screen.getByText(/welcome back/i).closest(".welcome-back")!;
    expect(banner.textContent).toMatch(/left off in chapter IV/i);
    // announced RELIABLY: folded into the freshly-opened dialog's accessible name, NOT a sibling
    // live region mounted already-populated (which screen readers drop — a11y review HIGH)
    expect(screen.getByRole("dialog").getAttribute("aria-label")).toMatch(/welcome back.*chapter IV/i);
  });

  test("no welcome-back framing on a normal (deliberate) open", async () => {
    mocks.catchMeUp.mockResolvedValue(recap);
    render(<CatchMeUp bookId="b" bookmark={4} totalAtoms={96} onClose={() => {}} />);
    await screen.findByText(/Fyodor married twice/);
    expect(screen.queryByText(/welcome back/i)).toBeNull();
    expect(screen.getByRole("dialog").getAttribute("aria-label")).not.toMatch(/welcome back/i);
  });
});
