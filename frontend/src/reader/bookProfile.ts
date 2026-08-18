import type { BookType } from "../api";

export type BookPresentation = {
  unit: "chapter" | "scene" | "section";
  recapLink: string;
  codexLink: string;
  codexTitle: string;
  breakdownTitle: string;
  peopleMode: "primary" | "conditional";
  peopleLabel: string;
  connectionsLabel: string;
  description: string;
};

const neutral = (description: string): BookPresentation => ({
  unit: "section",
  recapLink: "what you've read",
  codexLink: "reading notes",
  codexTitle: "reading notes",
  breakdownTitle: "what you've read, section by section",
  peopleMode: "conditional",
  peopleLabel: "people mentioned",
  connectionsLabel: "connections",
  description,
});

export function presentationFor(bookType: BookType): BookPresentation {
  if (bookType === "novel") {
    return {
      unit: "chapter",
      recapLink: "the story so far",
      codexLink: "the codex",
      codexTitle: "the codex",
      breakdownTitle: "the story so far, chapter by chapter",
      peopleMode: "primary",
      peopleLabel: "cast so far",
      connectionsLabel: "open threads",
      description: "A conventional narrative profile.",
    };
  }
  if (bookType === "drama") {
    return {
      unit: "section",
      recapLink: "the action so far",
      codexLink: "reading notes",
      codexTitle: "reading notes",
      breakdownTitle: "the action so far, scene by scene",
      peopleMode: "primary",
      peopleLabel: "people so far",
      connectionsLabel: "active ties",
      description: "Detected as drama; action and people remain useful without assuming novel chapters.",
    };
  }
  if (bookType === "anthology") {
    return neutral("Detected as a collection; sections may not share one plot or stable cast.");
  }
  if (bookType === "poetry") {
    return neutral("Detected as verse; people and plot sections appear only when grounded data exists.");
  }
  if (bookType === "nonfiction") {
    return neutral("Detected as nonfiction; notes emphasize sections and grounded takeaways.");
  }
  if (bookType === "reference") {
    return neutral("Detected as reference material; notes emphasize sections and grounded takeaways.");
  }
  return neutral(
    "This book could not be classified confidently; neutral labels avoid assuming a plot or stable cast.",
  );
}
