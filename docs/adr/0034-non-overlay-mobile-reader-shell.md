# ADR 0034: Mobile companion is a non-overlaying reader sheet

## Status

Accepted — 2026-07-21

## Context

The desktop reader binds the book and companion into two side-by-side leaves. On a phone, stacking the entire companion below the book permanently consumes the reading viewport, while a fixed overlay hides text and can make the reader lose its place when dismissed.

## Decision

At phone widths, the reader remains a two-row grid: the book owns the remaining viewport and the companion owns an intrinsic bottom row. The companion is collapsed to a 52-pixel handle by default. Expanding it grows the second grid row instead of positioning content over the book, so it never covers the page and never unmounts or replaces the Foliate view.

The responsive media query is reflected into React state so collapsed content is genuinely hidden from assistive technology. The handle exposes native `aria-expanded` and `aria-controls` state. Opening focuses the sheet heading; Escape closes the sheet and restores focus to the handle. Desktop keeps the existing complementary landmark without the mobile handle.

The shell uses dynamic viewport units and safe-area insets. Mobile header, navigation, and sheet controls have at least 44-pixel targets, and the sheet contains its own bounded scrolling and overscroll behavior. Reduced-motion preferences continue to disable decorative transitions.

## Consequences

- The book remains visible and mounted while the companion opens and closes.
- The sheet consumes viewport space instead of obscuring prose.
- Keyboard and assistive-technology state match the visual state.
- Phone cutouts and home indicators do not cover primary controls.
- Pagination may reflow when available height changes, but the same live Foliate view retains its navigation state and current location.
