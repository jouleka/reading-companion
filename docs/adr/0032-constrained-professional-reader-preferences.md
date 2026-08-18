# ADR 0032 — Reader appearance is a constrained per-owner/book preset contract

**Status:** Accepted (2026-07-21)
**Ticket:** LIT-52 / UX-2

## Context

Long-form reading needs adjustable text without exposing arbitrary CSS, turning the running head into
a settings dashboard, or rendering once with defaults and visibly repaginating after synchronized
preferences arrive. Hosted schema groundwork already reserved an owner/book `reader_preferences` row,
but its nullable numeric/free-form fields had no API or reader integration.

## Decision

The existing owner/book preference row is migrated to closed presets for text size, line height,
measure, theme, margins, and typeface. Legacy numeric/free-form values map to the nearest safe preset;
the JSON extension object is fixed empty. The row retains its composite book foreign key, forced RLS,
explicit repository owner predicates, and a monotonic preference version. Authenticated GET returns
sensible defaults without forcing a row; CSRF-protected PUT validates the whole object and upserts it.

The browser keeps the last validated value in a per-book cache for immediate paint and reconciles it
with the owner-scoped server value during book loading. The synchronized value is applied to Foliate's
iframe stylesheet and paginator `max-inline-size`, `margin`, and `gap` attributes after `open` but
before the first `init`/navigation, avoiding a default-layout flash. Later changes apply in place and
travel through one serialized, debounced whole-object saver with reconnect retry and keepalive drain
on teardown. Local mode uses the same validated cache when its older API returns `404`.

The compact running-head control expands to six native radio groups. It reports save state, closes on
Escape with focus restoration, remains usable by keyboard and screen reader, collapses to one column
at narrow/zoomed viewports, and uses fixed paper, sepia, night, or system palettes whose text and link
contrasts are tested at WCAG AA.

## Consequences

- Preferences synchronize between devices for the same owned book without accepting arbitrary CSS.
- Publisher typography remains available; serif and system-sans overrides are explicit reversible
  choices.
- Pagination geometry is controlled through Foliate's existing public observed attributes, with no
  closed-shadow-root mutation or vendor fork.
- Preference changes may repaginate the current section, but Foliate preserves its current anchor; the
  initial synchronized value is always installed before the first visible section.
- Future preference options require a schema, API literal, frontend preset, migration, and contrast/
  accessibility evidence rather than an unreviewed JSON key.
