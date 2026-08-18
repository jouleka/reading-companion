import { axe } from "jest-axe";

/** Run axe against the WCAG 2.1 A/AA conformance rules only — LIT-16's stated target. Components are
 * tested in isolation, so the best-practice PAGE-structure rules (page-has-heading-one,
 * landmark-one-main, region) don't apply to a fragment and would be false positives; the full-page
 * landmark/heading structure is asserted directly by role queries instead.
 *
 * color-contrast (1.4.3) is DISABLED here on purpose: axe's contrast rule needs a canvas 2D context
 * (jsdom has none), so it would silently no-op — disabling it makes that explicit rather than
 * incidental. Contrast is verified by direct measurement instead (see docs/a11y.md: every text/UI
 * pair clears AA), re-checkable live via getComputedStyle. So these checks assert STRUCTURE/ARIA. */
export const axeAA = (el: Element) =>
  axe(el, {
    runOnly: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"],
    rules: { "color-contrast": { enabled: false } },
  });
