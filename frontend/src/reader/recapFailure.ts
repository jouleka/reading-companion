function codeOf(error: unknown): string | null {
  if (!error || typeof error !== "object") return null;
  const code = (error as { code?: unknown }).code;
  return typeof code === "string" ? code : null;
}

export function recapFailureMessage(error: unknown, surface: "hero" | "sidebar"): string {
  const code = codeOf(error);
  if (code === "recap_timeout")
    return "This recap is taking longer than expected. You can keep reading and check again shortly; a cleared recap will be saved for next time.";
  if (code === "session_changed")
    return "Your reading session changed. Close this panel and reopen the book.";
  if (error && typeof error === "object" && (error as { status?: number }).status === 429) {
    const message = error instanceof Error ? error.message : "";
    return /ceiling|budget/i.test(message)
      ? "The recap reached this book’s AI budget limit. Your chapter notes and reading position are still available."
      : "The AI provider is busy. You can keep reading and try the recap again shortly.";
  }
  if (code === "provider_authentication_failed") {
    return "The AI companion is offline because the configured provider credentials were rejected. Your Codex is still available.";
  }
  if (code === "provider_rate_limited") {
    return "The AI companion is temporarily rate limited. Your Codex is still available.";
  }
  if (code === "provider_unavailable") {
    return "The AI companion is temporarily unavailable. Your Codex is still available.";
  }
  return surface === "hero"
    ? "The recap did not pass the spoiler gate — try again in a moment."
    : "The recap did not pass the spoiler gate — it will retry on your next page.";
}
