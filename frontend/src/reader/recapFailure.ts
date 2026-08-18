function codeOf(error: unknown): string | null {
  if (!error || typeof error !== "object") return null;
  const code = (error as { code?: unknown }).code;
  return typeof code === "string" ? code : null;
}

export function recapFailureMessage(error: unknown, surface: "hero" | "sidebar"): string {
  const code = codeOf(error);
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
