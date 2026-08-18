/** The position mapping is SPOILER-RELEVANT: a section that maps to a LATER atom than the reader is
 * actually in inflates the reported offset, the server bookmark ratchets (SQL MAX), and future
 * chapters unlock. Every ambiguity here must resolve to "report nothing" (-1 / null) — an
 * under-report is always safe, an overshoot never is. */
import { describe, expect, test } from "vitest";
import type { Atom } from "../api";
import { buildSectionAtomMap, offsetForAtom } from "./offset";

const atoms = (hrefs: string[], char_len = 1000): Atom[] =>
  hrefs.map((h, i) => ({ ordinal: i + 1, href: h, title: "", part_label: "", char_len }));

describe("buildSectionAtomMap", () => {
  test("exact 1:1 mapping, tolerant of ./ and / prefixes", () => {
    const a = atoms(["OEBPS/c1.html", "OEBPS/c2.html"]);
    expect(buildSectionAtomMap(a, ["./OEBPS/c1.html", "/OEBPS/c2.html"])).toEqual([0, 1]);
  });

  test("front matter with no atom maps to -1 (never guesses)", () => {
    const a = atoms(["OEBPS/c1.html", "OEBPS/c2.html"]);
    expect(buildSectionAtomMap(a, ["OEBPS/wrap0000.html", "OEBPS/c1.html", "OEBPS/c2.html"]))
      .toEqual([-1, 0, 1]);
  });

  test("SPOILER: a root-level front-matter file must NOT suffix-steal a late atom whose real section exists", () => {
    // the A-F1 attack: spine has front matter "intro.html" AND the real chapter "text/intro.html";
    // naive suffix matching maps the front matter onto the chapter -> huge offset at 0 chars read
    const a = atoms(["text/ch1.html", "text/intro.html"]);
    const map = buildSectionAtomMap(a, ["intro.html", "text/ch1.html", "text/intro.html"]);
    expect(map).toEqual([-1, 0, 1]);
  });

  test("global prefix difference falls back to suffix matching (OPF-relative atoms)", () => {
    const a = atoms(["c1.html", "c2.html"]);
    expect(buildSectionAtomMap(a, ["OEBPS/c1.html", "OEBPS/c2.html"])).toEqual([0, 1]);
  });

  test("two sections that would suffix-claim the same atom both get -1 (ambiguity = no report)", () => {
    const a = atoms(["ch1.html"]);
    expect(buildSectionAtomMap(a, ["part1/ch1.html", "part2/ch1.html"])).toEqual([-1, -1]);
  });

  test("percent-encoded manifest hrefs match foliate's decoded section ids", () => {
    const a = atoms(["OEBPS/my%20chapter.html"]);
    expect(buildSectionAtomMap(a, ["OEBPS/my chapter.html"])).toEqual([0]);
  });

  test("anchor-mode: all atoms of one file collapse to the FIRST (under-report, documented residual)", () => {
    const a = atoms(["main.html#ch1", "main.html#ch2", "main.html#ch3"]);
    expect(buildSectionAtomMap(a, ["main.html"])).toEqual([0]);
  });

  test("empty section href maps to -1", () => {
    expect(buildSectionAtomMap(atoms(["c1.html"]), [""])).toEqual([-1]);
  });

  test("query strings are stripped (foliate's resolveURL clears .search; the OPF may carry one)", () => {
    const a = atoms(["OEBPS/c1.html?v=2"]);
    expect(buildSectionAtomMap(a, ["OEBPS/c1.html"])).toEqual([0]);
  });
});

describe("offsetForAtom", () => {
  const a = atoms(["c1.html", "c2.html", "c3.html"]); // 1000 chars each

  test("conservative: offset = prior atoms + start of the visible page", () => {
    expect(offsetForAtom(a, 1, 3, 10)).toBe(1000 + 200); // page 3 of 10 -> 2/10 through
  });

  test("atom -1 reports nothing", () => {
    expect(offsetForAtom(a, -1, 1, 10)).toBeNull();
  });

  test("last page of a section never completes the chapter", () => {
    const o = offsetForAtom(a, 1, 10, 10);
    expect(o).not.toBeNull();
    expect(o!).toBeLessThan(2000); // strictly before atom 3's start
  });

  test("pre-layout zeros degrade to the atom start", () => {
    expect(offsetForAtom(a, 1, 0, 0)).toBe(1000);
  });

  test("NaN/Infinity layout states never produce NaN or an overshoot", () => {
    for (const [page, pages] of [[NaN, NaN], [Infinity, Infinity], [5, NaN], [NaN, 10], [3, Infinity]]) {
      const o = offsetForAtom(a, 1, page, pages);
      expect(o).toBe(1000); // degrade to the atom start, never null-body PUTs, never NaN
    }
  });

  test("page beyond pages clamps down (resize transient), still inside the atom", () => {
    const o = offsetForAtom(a, 1, 15, 10);
    expect(o!).toBeGreaterThanOrEqual(1000);
    expect(o!).toBeLessThan(2000);
  });
});
