/** LIT-29/30: matching bookmark-bounded cast names in recap prose so they render as clickable name
 * affordances, each carrying its entity_id (LIT-30) so a click opens the one character card.
 * Spoiler-safe by construction — only names from the clamped `cast` are ever wrapped. */
import { describe, expect, test } from "vitest";
import { wrapNames } from "./names";

const joined = (segs: { text: string }[]) => segs.map((s) => s.text).join("");
const chips = (segs: { text: string; entityId: number | null }[]) =>
  segs.filter((s) => s.entityId != null).map((s) => ({ text: s.text, id: s.entityId }));
const cast = (pairs: [string, number][]) => pairs.map(([name, entity_id]) => ({ name, entity_id }));

describe("wrapNames", () => {
  test("wraps a cast name with its entity_id, leaves the rest plain (round-trips the original)", () => {
    const segs = wrapNames("Aldric met Berenice at the forge.", cast([["Aldric", 1], ["Berenice", 2]]));
    expect(joined(segs)).toBe("Aldric met Berenice at the forge.");
    expect(chips(segs)).toEqual([{ text: "Aldric", id: 1 }, { text: "Berenice", id: 2 }]);
  });

  test("matches on word boundaries — 'Ivan' does not match inside 'Ivanovna'", () => {
    const segs = wrapNames("Ivanovna spoke to Ivan.", cast([["Ivan", 7]]));
    expect(chips(segs)).toEqual([{ text: "Ivan", id: 7 }]);
  });

  test("prefers the longest name so a full name wins over its parts, with its id", () => {
    const segs = wrapNames("Fyodor Pavlovitch laughed.", cast([["Fyodor", 3], ["Fyodor Pavlovitch", 5]]));
    expect(chips(segs)).toEqual([{ text: "Fyodor Pavlovitch", id: 5 }]);
  });

  test("ignores purely lowercase epithets (no capital) so they are not clickable", () => {
    const segs = wrapNames("the narrator watched Aldric.", cast([["the narrator", 9], ["Aldric", 1]]));
    expect(chips(segs)).toEqual([{ text: "Aldric", id: 1 }]);
  });

  test("keeps a named epithet that carries a proper noun", () => {
    const segs = wrapNames("Then the elder Zossima spoke.", cast([["the elder Zossima", 4]]));
    expect(chips(segs)).toEqual([{ text: "the elder Zossima", id: 4 }]);
  });

  test("handles accented names (Unicode-safe boundaries)", () => {
    const segs = wrapNames("Adelaïda ran off.", cast([["Adelaïda", 6]]));
    expect(chips(segs)).toEqual([{ text: "Adelaïda", id: 6 }]);
  });

  test("wraps names from scripts without letter case", () => {
    const segs = wrapNames("阿廖沙 spoke with Дмитрий.", cast([["阿廖沙", 8], ["Дмитрий", 9]]));
    expect(chips(segs)).toEqual([{ text: "阿廖沙", id: 8 }, { text: "Дмитрий", id: 9 }]);
  });

  test("no cast yields a single plain segment (never throws)", () => {
    expect(wrapNames("nothing to match.", [])).toEqual([
      { text: "nothing to match.", name: null, entityId: null },
    ]);
  });

  test("a name appearing twice is wrapped both times with its id", () => {
    const segs = wrapNames("Aldric found Aldric.", cast([["Aldric", 1]]));
    expect(chips(segs)).toEqual([{ text: "Aldric", id: 1 }, { text: "Aldric", id: 1 }]);
  });
});
