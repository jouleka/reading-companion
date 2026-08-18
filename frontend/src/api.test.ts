import { afterEach, describe, expect, test, vi } from "vitest";

import { api } from "./api";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("API errors", () => {
  test("preserves a structured provider error code without exposing raw response details", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      detail: {
        code: "provider_authentication_failed",
        message: "The AI provider rejected the configured credentials.",
      },
    }), { status: 503, headers: { "content-type": "application/json" } }));

    await expect(api.catchMeUp("b", 4)).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
      code: "provider_authentication_failed",
      message: "The AI provider rejected the configured credentials.",
    });
  });
});

describe("Reader navigation reads", () => {
  test("encodes owner-scoped in-book search queries and bounds the result count", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      as_of_chapter: 1, hits: [],
    }), { status: 200, headers: { "content-type": "application/json" } }));
    await api.searchBook("book", "light & shadow", 12);
    expect(fetch).toHaveBeenCalledWith(
      "/api/books/book/search?q=light+%26+shadow&limit=12",
    );
  });
});

describe("Hosted writes", () => {
  test("copies the non-HttpOnly CSRF cookie into provider-setting writes", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=csrf-token");
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      id: "setting", capability: "extraction", validation_status: "unchecked",
    }), { status: 200, headers: { "content-type": "application/json" } }));

    await api.putProviderSetting("extraction", {
      provider: "openai-compatible",
      model: "gpt-4o-mini",
      credential_id: "credential",
      base_url: "https://api.openai.com/v1",
    });
    expect(fetch).toHaveBeenCalledWith("/api/provider-settings/extraction", expect.objectContaining({
      method: "PUT",
      headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
    }));
  });

  test("accepts an empty 204 response when deleting a credential", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=csrf-token");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));
    await expect(api.deleteCredential("credential")).resolves.toBeUndefined();
  });

  test("sends constrained reader preferences to the owner/book route with CSRF", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=csrf-token");
    const preferences = {
      font_size: "large" as const,
      line_height: "comfortable" as const,
      measure: "balanced" as const,
      theme: "night" as const,
      margins: "balanced" as const,
      typeface: "serif" as const,
    };
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      ...preferences, preference_version: 2,
    }), { status: 200, headers: { "content-type": "application/json" } }));
    await api.putReaderPreferences("book", preferences);
    expect(fetch).toHaveBeenCalledWith("/api/books/book/preferences", expect.objectContaining({
      method: "PUT",
      headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
      body: JSON.stringify(preferences),
    }));
  });

  test("writes portable reader anchors with CSRF and deletes marks through their typed route", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=csrf-token");
    const mark = {
      id: "mark", kind: "highlight" as const,
      anchor: { cfi: "epubcfi(/6/2!/4/2,/1:0,/1:4)", atom: 1 },
      color: "yellow" as const, selected_text: "text", body: null, label: null,
      version: 1, created_at: "now", updated_at: "now",
    };
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify(mark), {
        status: 201, headers: { "content-type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    await api.createHighlight("book", {
      anchor: mark.anchor, color: "yellow", selected_text: "text",
    });
    expect(fetch).toHaveBeenNthCalledWith(1, "/api/books/book/highlights", expect.objectContaining({
      method: "POST",
      headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
      body: JSON.stringify({ anchor: mark.anchor, color: "yellow", selected_text: "text" }),
    }));
    await api.deleteReaderMark("book", mark);
    expect(fetch).toHaveBeenNthCalledWith(2, "/api/books/book/highlights/mark", expect.objectContaining({
      method: "DELETE",
      headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
    }));
  });

  test("queues an optimistic portable highlight when the network is offline", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const mark = await api.createHighlight("book", {
      anchor: { cfi: "epubcfi(/6/2!/4/2,/1:0,/1:4)", atom: 1 },
      color: "green",
      selected_text: "survives the train tunnel",
    });
    expect(mark).toMatchObject({
      kind: "highlight",
      color: "green",
      selected_text: "survives the train tunnel",
      pending: true,
    });
    await expect(api.readerMarks("book")).rejects.toThrow("offline");
  });

  test("posts selected-text and chapter-closeout assistance with CSRF and bounded inputs", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=csrf-token");
    const payload = JSON.stringify({
      insufficient_evidence: true,
      cost: { currency: "USD", usd: "0", calls: [] },
    });
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(payload, {
        status: 200, headers: { "content-type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(payload, {
        status: 200, headers: { "content-type": "application/json" },
      }));
    await api.selectionAction("book", {
      action: "translate", text: "Bonjour", atom: 2, cfi: "epubcfi(/6/4)",
    });
    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "/api/books/book/selection-action",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
        body: JSON.stringify({
          action: "translate", text: "Bonjour", atom: 2, cfi: "epubcfi(/6/4)",
          target_language: "English",
        }),
      }),
    );
    await api.chapterCloseout("book", 2);
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/books/book/chapter-closeout",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
        body: JSON.stringify({ chapter: 2 }),
      }),
    );
  });

  test("posts an exact-frontier memory correction with CSRF", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=csrf-token");
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      as_of_chapter: 3, correction_id: 7, target_entity_id: 8, items: [],
    }), { status: 200, headers: { "content-type": "application/json" } }));
    const correction = {
      source_entity_id: 4,
      canonical_name: "Wilhelmina Harker",
      reason: "The full name is established in the chapters already read.",
      bookmark: 3,
    };
    await api.correctMemory("book", correction);
    expect(fetch).toHaveBeenCalledWith(
      "/api/books/book/memory-corrections",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
        body: JSON.stringify(correction),
      }),
    );
  });
});
