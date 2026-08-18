import { describe, expect, test } from "vitest";
import { roman } from "./roman";

describe("roman", () => {
  test("small numerals", () => {
    expect(roman(1)).toBe("I");
    expect(roman(4)).toBe("IV");
    expect(roman(9)).toBe("IX");
  });

  test("does not run out at X (Karamazov has 96 chapters)", () => {
    expect(roman(11)).toBe("XI");
    expect(roman(44)).toBe("XLIV");
    expect(roman(96)).toBe("XCVI");
  });

  test("zero renders the em-dash placeholder", () => {
    expect(roman(0)).toBe("–");
  });
});
