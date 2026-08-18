/** The jump guard (SPOILER-RELEVANT): the server bookmark is a permanent ratchet (SQL MAX), so a
 * relocation that lands beyond the next chapter — a ToC link, a goTo — must never auto-report; the
 * real Karamazov front-matter ToC links to every chapter, so one misclick would otherwise mark ~the
 * whole book read, irreversibly. Progressive reading only ever advances into the NEXT atom.
 *
 * `lastAtom` = the atom of the last accepted report (follows the reader backward too);
 * `maxAtom`  = the furthest atom ever accepted (returning to already-read ground is never a jump). */
export function jumpsAhead(target: number, lastAtom: number, maxAtom: number): boolean {
  if (target < 0) return false; // unmatched sections report nothing anyway
  return target > Math.max(lastAtom, maxAtom) + 1;
}

/** Should the global ArrowLeft/ArrowRight page-turn be suppressed for this key event's target?
 * Yes when focus is in a control/region where arrows already mean something (a text field, the
 * scrollable companion, the confirm dialog). Non-Element targets (window/document/null — as when a
 * key event is synthesised, or focus is nowhere) are safe and never block. */
const ARROW_EXEMPT =
  "input, textarea, select, [contenteditable], [role='alertdialog'], [role='dialog'], .companion";
export function arrowPagingBlocked(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest(ARROW_EXEMPT) != null;
}

/** The confirmation-token protocol: a pending token is spent ONLY by the far relocate it was issued
 * for — an incidental near relocate (a resize firing mid-goTo while the target section still loads)
 * must pass through without touching it, or the confirmed landing arrives token-less and tracking
 * dies at the destination. */
export function admitRelocation(
  target: number,
  lastAtom: number,
  maxAtom: number,
  allowJump: boolean,
): { admit: boolean; spendToken: boolean } {
  const far = jumpsAhead(target, lastAtom, maxAtom);
  if (!far) return { admit: true, spendToken: false };
  return allowJump ? { admit: true, spendToken: true } : { admit: false, spendToken: false };
}
