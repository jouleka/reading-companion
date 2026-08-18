/** LIT-29/30 — match bookmark-bounded cast names in recap prose so they render as clickable name
 * affordances, each carrying its `entityId` (LIT-30) so a click opens the one character card.
 * Spoiler-safe by construction: only the clamped `cast` (all revealed_at <= bookmark) is ever matched,
 * so a wrapped name can never be a future entity. Pure string work — no network, no frontier logic. */
import type { CastMember } from "../api";

export type NameSegment = { text: string; name: string | null; entityId: number | null };

/** A cast entry earns an affordance if it carries an uppercase letter, or if it is a multi-letter
 * name in a script without case. Lowercase generic epithets remain unwrapped. */
function properOnly(cast: CastMember[]): CastMember[] {
  return cast.filter((c) => {
    if (/\p{Lu}/u.test(c.name)) return true;
    const letters = c.name.match(/\p{L}/gu) ?? [];
    return letters.length >= 2 && !/\p{Ll}/u.test(c.name);
  });
}

export function wrapNames(text: string, cast: CastMember[]): NameSegment[] {
  const proper = properOnly(cast);
  if (proper.length === 0) return [{ text, name: null, entityId: null }];
  // dedup by name (a clean surface-form -> id map), longest first so a full name wins over its parts
  const byName = new Map<string, number>();
  for (const c of proper) if (!byName.has(c.name)) byName.set(c.name, c.entity_id);
  const sorted = [...byName.keys()].sort((a, b) => b.length - a.length);
  const esc = sorted.map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  // Unicode-safe boundaries: not flanked by a letter, so "Ivan" never matches inside "Ivanovna" and
  // accented names ("Adelaïda") match where a plain \b would fail.
  const re = new RegExp(`(?<!\\p{L})(?:${esc.join("|")})(?!\\p{L})`, "gu");
  const segs: NameSegment[] = [];
  let last = 0;
  for (const m of text.matchAll(re)) {
    const i = m.index ?? 0;
    if (i > last) segs.push({ text: text.slice(last, i), name: null, entityId: null });
    segs.push({ text: m[0], name: m[0], entityId: byName.get(m[0]) ?? null });
    last = i + m[0].length;
  }
  if (last < text.length) segs.push({ text: text.slice(last), name: null, entityId: null });
  return segs.length ? segs : [{ text, name: null, entityId: null }];
}
