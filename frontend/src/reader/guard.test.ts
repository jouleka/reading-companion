/** The jump guard (SPOILER-RELEVANT): the server bookmark is a permanent ratchet, so a relocation
 * that lands far ahead of where the reader actually is (a ToC link, a goTo) must never auto-report.
 * Progressive reading only ever advances into the NEXT atom; anything further is a jump that needs
 * explicit confirmation. */
import { describe, expect, test } from "vitest";
import { admitRelocation, arrowPagingBlocked, jumpsAhead } from "./guard";

describe("jumpsAhead(target, lastAtom, maxAtom)", () => {
  test("paging within the current atom is never a jump", () => {
    expect(jumpsAhead(3, 3, 3)).toBe(false);
  });

  test("entering the next atom (chapter completion) is never a jump", () => {
    expect(jumpsAhead(4, 3, 3)).toBe(false);
  });

  test("backward relocations are never jumps", () => {
    expect(jumpsAhead(0, 3, 3)).toBe(false);
    expect(jumpsAhead(2, 3, 3)).toBe(false);
  });

  test("a ToC leap deep ahead IS a jump", () => {
    expect(jumpsAhead(94, 3, 3)).toBe(true);
    expect(jumpsAhead(5, 3, 3)).toBe(true); // even two chapters ahead
  });

  test("after paging back, returning anywhere up to the furthest-read point is not a jump", () => {
    expect(jumpsAhead(5, 1, 5)).toBe(false); // back at ch2, returning to ch6 (already read)
    expect(jumpsAhead(6, 1, 5)).toBe(false); // the chapter after the furthest completed
    expect(jumpsAhead(7, 1, 5)).toBe(true);  // beyond it
  });

  test("an unmatched target (-1) is never a jump (nothing will be reported anyway)", () => {
    expect(jumpsAhead(-1, 3, 3)).toBe(false);
  });
});

describe("admitRelocation (the confirmation token protocol)", () => {
  test("a near relocate NEVER spends a pending token (a resize mid-goTo must not eat the confirmation)", () => {
    // pass-2 F1: while a confirmed goTo loads its target, an incidental resize fires a relocate at
    // the CURRENT atom; spending the token there kills tracking at the landing point forever
    expect(admitRelocation(3, 3, 3, true)).toEqual({ admit: true, spendToken: false });
    expect(admitRelocation(4, 3, 3, true)).toEqual({ admit: true, spendToken: false });
  });

  test("a far relocate with the token is admitted and spends it", () => {
    expect(admitRelocation(90, 3, 3, true)).toEqual({ admit: true, spendToken: true });
  });

  test("a far relocate without the token is blocked", () => {
    expect(admitRelocation(90, 3, 3, false)).toEqual({ admit: false, spendToken: false });
  });

  test("ordinary relocations without any token are admitted", () => {
    expect(admitRelocation(4, 3, 3, false)).toEqual({ admit: true, spendToken: false });
  });
});

describe("arrowPagingBlocked (the global arrow-key guard)", () => {
  test("a non-Element target (window/document/null) never throws and never blocks", () => {
    expect(arrowPagingBlocked(window)).toBe(false);
    expect(arrowPagingBlocked(document)).toBe(false);
    expect(arrowPagingBlocked(null)).toBe(false);
  });

  test("a plain element in the book does not block paging", () => {
    const div = document.createElement("div");
    document.body.appendChild(div);
    expect(arrowPagingBlocked(div)).toBe(false);
    div.remove();
  });

  test("a text control blocks paging (arrows move the caret there)", () => {
    const input = document.createElement("input");
    document.body.appendChild(input);
    expect(arrowPagingBlocked(input)).toBe(true);
    input.remove();
  });

  test("an element inside the companion or an alertdialog blocks paging", () => {
    const aside = document.createElement("aside");
    aside.className = "companion";
    const child = document.createElement("span");
    aside.appendChild(child);
    document.body.appendChild(aside);
    expect(arrowPagingBlocked(child)).toBe(true);
    aside.remove();

    const dlg = document.createElement("div");
    dlg.setAttribute("role", "alertdialog");
    const btn = document.createElement("button");
    dlg.appendChild(btn);
    document.body.appendChild(dlg);
    expect(arrowPagingBlocked(btn)).toBe(true);
    dlg.remove();
  });

  test("an element inside a modal dialog (the hero) blocks paging the book behind it", () => {
    // the hero is role="dialog"; an arrow there must never reach the book underneath the scrim
    const dlg = document.createElement("div");
    dlg.setAttribute("role", "dialog");
    const btn = document.createElement("button");
    dlg.appendChild(btn);
    document.body.appendChild(dlg);
    expect(arrowPagingBlocked(btn)).toBe(true);
    dlg.remove();
  });
});
