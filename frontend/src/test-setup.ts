import { toHaveNoViolations } from "jest-axe";
import { expect } from "vitest";

// jest-axe's matcher, registered for the whole frontend suite (a11y is a standing gate now)
expect.extend(toHaveNoViolations);
