/** CastList (LIT-31): the cast so far, down the codex's outer margin — an ARIA listbox with a ROVING
 * TABINDEX (absorbs LIT-28). Because the graph canvas is opaque to assistive tech, this list IS the
 * accessible, keyboard-operable way to navigate the cast: exactly one option is tabbable, Arrow/Home/End
 * move the roving focus, and Enter/Space/click selects a character (re-centering the focus graph on
 * them). The current focus is `aria-selected`. */
import { useEffect, useRef, useState } from "react";
import type { GraphNode } from "../api";

export function CastList({
  characters,
  focusId,
  onFocus,
}: {
  characters: GraphNode[];
  focusId: number | null;
  onFocus: (entityId: number) => void;
}) {
  const [roving, setRoving] = useState(() => {
    const i = characters.findIndex((c) => c.entity_id === focusId);
    return i < 0 ? 0 : i;
  });
  const itemRefs = useRef<(HTMLLIElement | null)[]>([]);
  const listRef = useRef<HTMLUListElement>(null);
  const prevLenRef = useRef(characters.length);
  // Captured DURING render, before React mutates the DOM: does this listbox hold focus right now?
  // While the shrunk list renders, the committed DOM still shows the OLD options, so an option that
  // is about to be removed is still `document.activeElement`. This distinguishes "focus will fall to
  // <body> because MY option is being removed" (repair it) from "focus was already on <body> for an
  // unrelated reason" (leave it) — the pass-2 regression where a signature-only gate yanked focus
  // into the list unrequested (WCAG 3.2.1).
  const heldFocusRef = useRef(false);
  heldFocusRef.current = listRef.current?.contains(document.activeElement) ?? heldFocusRef.current;

  // keep the roving index in range when the list shrinks (scrubbing back drops later-revealed cast),
  // and REPAIR DOM focus ONLY when a shrink orphaned focus THIS list was holding: removing the
  // focused option drops focus to <body>, stranding a keyboard user behind the open dialog (pass-1
  // HIGH, WCAG 2.4.3). Never steals focus parked on the scrubber / a chapter head (not orphaned) or
  // on <body> for an unrelated reason (the list didn't hold it — pass-2 fix).
  useEffect(() => {
    const clamped = Math.max(0, Math.min(roving, characters.length - 1));
    if (clamped !== roving) setRoving(clamped);
    const shrank = characters.length < prevLenRef.current;
    prevLenRef.current = characters.length;
    if (!shrank || characters.length === 0) return;
    const active = document.activeElement;
    const orphaned = !active || active === document.body || active === document.documentElement;
    if (heldFocusRef.current && orphaned) itemRefs.current[clamped]?.focus();
  }, [characters.length, roving]);

  const move = (i: number) => {
    const n = Math.max(0, Math.min(characters.length - 1, i));
    setRoving(n);
    itemRefs.current[n]?.focus();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); move(roving + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); move(roving - 1); }
    else if (e.key === "Home") { e.preventDefault(); move(0); }
    else if (e.key === "End") { e.preventDefault(); move(characters.length - 1); }
    else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      const c = characters[roving];
      if (c) onFocus(c.entity_id);
    }
  };

  if (characters.length === 0) return <p className="quiet cast-list-empty">No one yet.</p>;

  return (
    <ul
      className="cast-list"
      role="listbox"
      aria-label="the cast so far"
      ref={listRef}
      onKeyDown={onKeyDown}
    >
      {characters.map((c, i) => (
        <li
          key={c.entity_id}
          role="option"
          aria-selected={c.entity_id === focusId}
          tabIndex={i === roving ? 0 : -1}
          ref={(el) => {
            itemRefs.current[i] = el;
          }}
          className={`cast-item${c.entity_id === focusId ? " is-focus" : ""}`}
          onClick={() => onFocus(c.entity_id)}
        >
          {c.canonical_name}
          {c.type !== "character" ? ` (${c.type})` : ""}
        </li>
      ))}
    </ul>
  );
}
