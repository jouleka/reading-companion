/** The position mapping (ADR 0008 + ADR 0006): foliate relocations -> the MONOTONIC char offset the
 * frontier consumes. SPOILER-RELEVANT: an overshoot inflates the bookmark and could reveal a chapter
 * early, so every rule here is deliberately CONSERVATIVE — any ambiguity resolves to "report
 * nothing" (-1 / null); an under-report is always safe, an overshoot never is.
 *
 *   offset = sum(char_len of atoms BEFORE the current one) + ((page - 1) / pages) * char_len(atom)
 *
 * (page-1)/pages = the START of the currently-visible page — only what has certainly been read. A
 * chapter therefore completes exactly when the reader relocates INTO the next section (offset reaches
 * the next atom's start), matching LIT-12's whole-chapter-on-completion semantics. The server still
 * clamps/monotonizes (D-A10) — this is belt, not suspenders. */
import type { Atom } from "../api";

/** Strip the fragment + query string and leading ./ or /, and percent-DECODE: foliate decodes
 * section ids and clears .search (epub.js resolveURL) while manifest hrefs come raw from the OPF.
 * KNOWN residuals, all failing toward -1 (tracking silently dead, never an overshoot): files whose
 * names contain literal %-sequences (foliate pre-decodes section ids, so they decode twice against
 * the atom's once), and an anchor-mode file sharing a basename with a front-matter decoy (the
 * multi-claimant rule cancels both). */
export function normalizeHref(href: string): string {
  const clean = href.split("#")[0].split("?")[0].replace(/^\.?\//, "");
  try {
    return decodeURI(clean);
  } catch {
    return clean;
  }
}

/** Map every spine section to its manifest atom ONCE, seeing the whole spine — per-relocate string
 * guessing is what made suffix matching stealable (a root-level "intro.html" front-matter file
 * suffix-matched "text/intro.html" and reported ~the whole book read). Rules, all failing toward -1:
 *   1. exact matches win, and an exactly-claimed atom is off-limits to suffix claims;
 *   2. a section may suffix-claim only when all its candidate atoms are one file (anchor-mode
 *      collapses to the FIRST atom of the file = under-report, the documented ADR 0008 residual);
 *   3. two sections suffix-claiming the same atom cancel each other (can't tell which is real). */
export function buildSectionAtomMap(atoms: Atom[], sectionHrefs: string[]): number[] {
  const atomHrefs = atoms.map((a) => normalizeHref(a.href));
  const secHrefs = sectionHrefs.map(normalizeHref);

  const result = secHrefs.map((s) => (s ? atomHrefs.indexOf(s) : -1));
  const exactlyClaimed = new Set(result.filter((i) => i >= 0));

  const suffixClaims = new Map<number, number[]>(); // atom index -> claiming section indices
  secHrefs.forEach((s, si) => {
    if (result[si] >= 0 || !s) return;
    const candidates: number[] = [];
    atomHrefs.forEach((a, ai) => {
      if (a && (a.endsWith("/" + s) || s.endsWith("/" + a))) candidates.push(ai);
    });
    if (candidates.length === 0) return;
    const files = new Set(candidates.map((ai) => atomHrefs[ai]));
    if (files.size > 1) return; // different files both plausible: no report
    const target = candidates[0]; // anchor-mode: the file's FIRST atom (safe under-report)
    if (exactlyClaimed.has(target)) return; // the real section for that atom exists elsewhere
    suffixClaims.set(target, [...(suffixClaims.get(target) ?? []), si]);
  });
  for (const [target, claimants] of suffixClaims) {
    if (claimants.length === 1) result[claimants[0]] = target;
    // >1: ambiguous — every claimant stays -1
  }
  return result;
}

/** The conservative offset for a relocation inside atom `i`. Degenerate layout states (page/pages
 * 0, NaN, Infinity — observed live pre-layout) degrade to the atom START: the reader IS in this
 * atom, so its start has certainly been reached, and nothing further is claimed. */
export function offsetForAtom(atoms: Atom[], i: number, page: number, pages: number): number | null {
  if (i < 0 || i >= atoms.length) return null;
  let before = 0;
  for (let k = 0; k < i; k++) before += atoms[k].char_len;
  let fraction = 0;
  if (Number.isFinite(page) && Number.isFinite(pages) && pages >= 1) {
    const p = Math.min(Math.max(page, 1), pages);
    fraction = (p - 1) / pages; // the start of the visible page; always < 1
  }
  const offset = Math.floor(before + fraction * atoms[i].char_len);
  return Number.isFinite(offset) ? offset : null;
}
