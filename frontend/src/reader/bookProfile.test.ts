import { describe, expect, test } from "vitest";
import { presentationFor } from "./bookProfile";

describe("book-type presentation", () => {
  test("keeps the established novel vocabulary unchanged", () => {
    expect(presentationFor("novel")).toMatchObject({
      unit: "chapter",
      recapLink: "the story so far",
      codexLink: "the codex",
      peopleMode: "primary",
    });
  });

  test("uses honest neutral vocabulary for non-novel and unknown books", () => {
    expect(presentationFor("reference")).toMatchObject({
      unit: "section",
      recapLink: "what you've read",
      codexLink: "reading notes",
      peopleMode: "conditional",
    });
    expect(presentationFor("poetry").peopleMode).toBe("conditional");
    expect(presentationFor("unknown").description).toMatch(/could not be classified confidently/i);
  });

  test("drama retains people but calls the narrative action", () => {
    expect(presentationFor("drama")).toMatchObject({
      unit: "section",
      recapLink: "the action so far",
      peopleMode: "primary",
    });
  });
});
