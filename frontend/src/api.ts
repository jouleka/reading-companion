/** The backend client (ADR 0007 D-A11 routes). The client NEVER computes the spoiler frontier —
 * it reports (cfi, offset) and renders whatever the clamped server routes return. */

import {
  applyPendingMarks,
  pendingPosition,
  purgeOfflineTenant,
  queueMarkCreate,
  queueMarkDelete,
  queueMarkUpdate,
  queuePosition,
} from "./pwa/offline";

export type Book = { book_id: string; title: string; author: string | null };
export type BookType = "novel" | "anthology" | "drama" | "poetry" | "nonfiction" | "reference" | "unknown";
export type BookProfile = {
  book_type: BookType;
  confidence: number;
  detector_version: string;
  signals: string[];
};
export type Atom = { ordinal: number; href: string; title: string; part_label: string; char_len: number };
export type Manifest = {
  book_id: string;
  atom_set_version: string;
  mode: string;
  content_language: string;
  book_profile: BookProfile;
  atoms: Atom[];
};
export type Position = {
  bookmark: number; cfi: string | null; ingest_progress?: number; position_epoch: number; atoms?: number;
  completed_chapter?: number; current_offset?: number; high_water_offset?: number;
  position_version?: number; last_opened_at?: string; updated_at?: string; queued?: boolean;
};
export type PutPosition = {
  bookmark: number; current_chapter: number; chapter_progress: number; position_epoch: number;
  cfi?: string | null; completed_chapter?: number; current_offset?: number;
  high_water_offset?: number; position_version?: number; applied?: boolean; conflict?: string | null;
  queued?: boolean;
};
// a bookmark-bounded surface form -> its entity, so a clicked name opens the one card (LIT-30)
export type CastMember = { name: string; entity_id: number };
export type CatchMeUp = {
  // recap = the flowing "story so far" (hero); now = the tight "right now" one-liner (sidebar);
  // cast = the bookmark-bounded names the recap renders as clickable name affordances (LIT-29/30)
  recap: string | null; now: string | null; as_of_chapter: number;
  cast_size: number; open_threads: number; cast: CastMember[]; cached: boolean;
};
export type AskClaim = { text: string; citation_ids: number[] };
export type AskCitation = {
  id: number; ordinal: number; chapter_key: string; href: string; title: string; excerpt: string;
};
export type AskCost = {
  currency: "USD"; usd: string; input_tokens: number; output_tokens: number;
  pricing_known: boolean;
  calls: { provider: string; model: string; usd: string }[];
  payer: string;
};
export type AskAnswer = {
  as_of_chapter: number; insufficient_evidence: boolean; claims: AskClaim[];
  citations: AskCitation[]; cost: AskCost;
};
export type SelectionAction = "explain" | "define" | "translate";
export type SelectionAssistAnswer = {
  action: SelectionAction; as_of_chapter: number; insufficient_evidence: boolean;
  text: string | null; citation: (AskCitation & { cfi: string }) | null; cost: AskCost;
};
export type ChapterCloseout = AskAnswer & { chapter: number };
// a character's tie to another entity, phrased by `direction` relative to the queried character (LIT-30)
export type Tie = { entity_id: number; name: string; rel_type: string; label: string; direction: "in" | "out" };
export type Character = {
  as_of_chapter: number; entity_id: number; name: string; type: string;
  aliases: string[]; first_seen: number; status: unknown; ties: Tie[];
};
export type IngestStatus = { ingest_progress: number; status: string; flags: string[]; error: string | null };
export type GraphNode = {
  entity_id: number;
  canonical_name: string;
  type: string;
  revealed_at: number;
  aliases?: string[];
};
export type GraphEdge = {
  edge_id: number; src_entity: number; dst_entity: number;
  rel_type: string; label: string; revealed_at: number; invalid_at: number | null;
};
export type Graph = { as_of_chapter: number; characters: GraphNode[]; relationships: GraphEdge[] };
export type MemoryCorrection = {
  correction_id: number | string;
  kind: "replace" | "split" | "merge";
  effective_at: number;
  source_entities: { entity_id: number | string; name: string }[];
  target_entities: { entity_id: number | string; name: string }[];
  reason: string | null;
  recorded_at: string;
};
export type MemoryCorrections = { as_of_chapter: number; items: MemoryCorrection[] };
// the codex "story broken down" (LIT-31): a chapter + its highlights (who first appears, its events)
export type ChapterNote = {
  chapter_key: string; revealed_at: number; title: string; summary: string;
  new_characters: { entity_id: number; name: string }[]; events: string[];
};
export type Notes = { as_of_chapter: number; cast: CastMember[]; chapters: ChapterNote[] };
export type ProviderCapability = "extraction" | "synthesis" | "embedding" | "judge";
export type HostedProvider = "openai-compatible" | "anthropic" | "offline";
export type CredentialMetadata = {
  id: string; provider: Exclude<HostedProvider, "offline">; masked_label: string; key_version: string;
  created_at: string; rotated_at: string | null; disabled_at: string | null;
};
export type ReaderPreferences = {
  font_size: "small" | "book" | "large" | "x-large";
  line_height: "compact" | "comfortable" | "relaxed";
  measure: "narrow" | "balanced" | "wide";
  theme: "paper" | "sepia" | "night" | "system";
  margins: "compact" | "balanced" | "generous";
  typeface: "publisher" | "serif" | "sans";
  preference_version: number;
};
export type BookSearchHit = {
  ordinal: number; href: string; title: string; part_label: string; snippet: string; score: number;
};
export type BookSearch = { as_of_chapter: number; hits: BookSearchHit[] };
export type ReaderMarkAnchor = {
  cfi: string;
  atom: number;
  quote?: { exact: string; prefix: string; suffix: string };
};
type ReaderMarkBase = {
  id: string; anchor: ReaderMarkAnchor; version: number; created_at: string; updated_at: string;
  highlight_id?: string | null;
  pending?: boolean;
};
export type HighlightMark = ReaderMarkBase & {
  kind: "highlight"; color: "yellow" | "green" | "blue" | "pink"; selected_text: string;
  body: null; label: null;
};
export type AnnotationMark = ReaderMarkBase & {
  kind: "annotation"; body: string; color: null; selected_text: null; label: null;
};
export type BookmarkMark = ReaderMarkBase & {
  kind: "bookmark"; label: string | null; color: null; selected_text: null; body: null;
};
export type ReaderMark = HighlightMark | AnnotationMark | BookmarkMark;
export type ReaderMarks = { as_of_chapter: number; marks: ReaderMark[] };
export type ProviderSetting = {
  id: string; provider: HostedProvider; capability: ProviderCapability; credential_id: string | null;
  model: string; base_url: string | null; enabled: boolean;
  validation_status: "unchecked" | "ready" | "offline" | "invalid";
  validation_error_code: string | null; validated_at: string | null;
  created_at: string; updated_at: string;
};
export type ProviderRecommendation = { provider: HostedProvider; model: string; base_url: string | null };
export type ProviderSettingsPayload = {
  capabilities: ProviderCapability[]; providers: HostedProvider[];
  recommendations: Record<ProviderCapability, ProviderRecommendation>;
  recommendations_persisted: false; offline_behavior: string; cost_ownership: string;
  items: ProviderSetting[];
};
export type ProviderSettingInput = {
  provider: HostedProvider; model: string; credential_id: string | null; base_url?: string | null;
};
export type ProviderValidation = {
  status: "ready" | "offline" | "invalid";
  code: "ok" | "offline" | "invalid_credentials" | "unavailable_model" | "network_error" | "service_error";
  setting: ProviderSetting;
};

export class ApiError extends Error {
  status: number;
  code: string | null;

  constructor(message: string, status: number, code: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) {
    // surface the server's human message (FastAPI's {"detail": ...}), not raw status + JSON
    const text = await res.text();
    let msg = `the server said no (${res.status})`;
    let code: string | null = null;
    try {
      const detail = (JSON.parse(text) as { detail?: unknown }).detail;
      if (typeof detail === "string" && detail) msg = detail;
      if (detail && typeof detail === "object") {
        const structured = detail as { code?: unknown; message?: unknown };
        if (typeof structured.message === "string" && structured.message) msg = structured.message;
        if (typeof structured.code === "string" && structured.code) code = structured.code;
      }
    } catch {
      if (text) msg = text;
    }
    throw new ApiError(msg, res.status, code);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

function csrfHeaders(): Record<string, string> {
  const entry = document.cookie.split(";").map((item) => item.trim()).find(
    (item) => item.startsWith("__Host-litlet-csrf="),
  );
  if (!entry) return {};
  const value = entry.slice(entry.indexOf("=") + 1);
  return value ? { "X-CSRF-Token": decodeURIComponent(value) } : {};
}

function offlineFailure(error: unknown): boolean {
  return error instanceof TypeError || (typeof navigator !== "undefined" && !navigator.onLine);
}

function optimisticBase(anchor: ReaderMarkAnchor) {
  const timestamp = new Date().toISOString();
  return {
    id: `offline-${crypto.randomUUID()}`,
    anchor,
    version: 1,
    created_at: timestamp,
    updated_at: timestamp,
    pending: true,
  };
}

export const api = {
  books: () => fetch("/api/books").then((r) => j<Book[]>(r)),
  importBook: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch("/api/books", { method: "POST", headers: csrfHeaders(), body: fd }).then(
      (r) => j<{ book_id: string }>(r),
    );
  },
  manifest: (id: string) => fetch(`/api/books/${id}/manifest`).then((r) => j<Manifest>(r)),
  epubUrl: (id: string) => `/api/books/${id}/epub`,
  position: async (id: string) => {
    const value = await fetch(`/api/books/${id}/position`).then((r) => j<Position>(r));
    const queued = pendingPosition(id);
    if (!queued || (
      queued.position_epoch != null && Number(queued.position_epoch) !== value.position_epoch
    )) return value;
    const completed = Number(queued.completed_chapter ?? value.completed_chapter ?? value.bookmark);
    const offset = Number(queued.offset ?? value.current_offset ?? 0);
    return {
      ...value,
      bookmark: Math.max(value.bookmark, completed),
      completed_chapter: Math.max(value.completed_chapter ?? value.bookmark, completed),
      current_offset: offset,
      high_water_offset: Math.max(value.high_water_offset ?? 0, offset),
      cfi: typeof queued.cfi === "string" ? queued.cfi : value.cfi,
      queued: true,
    };
  },
  readerPreferences: (id: string) =>
    fetch(`/api/books/${id}/preferences`).then((r) => j<ReaderPreferences>(r)),
  searchBook: (id: string, query: string, limit = 20) => {
    const params = new URLSearchParams({ q: query, limit: String(limit) });
    return fetch(`/api/books/${id}/search?${params}`).then((r) => j<BookSearch>(r));
  },
  readerMarks: async (id: string) => {
    const value = await fetch(`/api/books/${id}/marks`).then((r) => j<ReaderMarks>(r));
    return { ...value, marks: applyPendingMarks(id, value.marks) };
  },
  readerMarksExportUrl: (id: string) => `/api/books/${id}/marks/export`,
  createHighlight: (
    id: string,
    value: { anchor: ReaderMarkAnchor; color: HighlightMark["color"]; selected_text: string },
  ) => fetch(`/api/books/${id}/highlights`, {
    method: "POST",
    headers: { "content-type": "application/json", ...csrfHeaders() },
    body: JSON.stringify(value),
  }).then((r) => j<HighlightMark>(r)).catch((error) => {
    if (!offlineFailure(error)) throw error;
    const mark: HighlightMark = {
      ...optimisticBase(value.anchor),
      kind: "highlight",
      color: value.color,
      selected_text: value.selected_text,
      body: null,
      label: null,
    };
    return queueMarkCreate(id, "highlights", value, mark) as HighlightMark;
  }),
  createAnnotation: (
    id: string,
    value: { anchor: ReaderMarkAnchor; body: string; highlight_id?: string },
  ) => fetch(`/api/books/${id}/annotations`, {
    method: "POST",
    headers: { "content-type": "application/json", ...csrfHeaders() },
    body: JSON.stringify(value),
  }).then((r) => j<AnnotationMark>(r)).catch((error) => {
    if (!offlineFailure(error)) throw error;
    const mark: AnnotationMark = {
      ...optimisticBase(value.anchor),
      kind: "annotation",
      body: value.body,
      highlight_id: value.highlight_id ?? null,
      color: null,
      selected_text: null,
      label: null,
    };
    return queueMarkCreate(id, "annotations", value, mark) as AnnotationMark;
  }),
  createBookmark: (id: string, value: { anchor: ReaderMarkAnchor; label?: string }) =>
    fetch(`/api/books/${id}/bookmarks`, {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify(value),
    }).then((r) => j<BookmarkMark>(r)).catch((error) => {
      if (!offlineFailure(error)) throw error;
      const mark: BookmarkMark = {
        ...optimisticBase(value.anchor),
        kind: "bookmark",
        label: value.label ?? null,
        color: null,
        selected_text: null,
        body: null,
      };
      return queueMarkCreate(id, "bookmarks", value, mark) as BookmarkMark;
    }),
  updateReaderMark: (id: string, mark: ReaderMark, value: string) => {
    const collection = `${mark.kind}s`;
    const key = mark.kind === "highlight" ? "color" : mark.kind === "annotation" ? "body" : "label";
    const body = { [key]: value };
    return fetch(`/api/books/${id}/${collection}/${mark.id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify(body),
    }).then((r) => j<ReaderMark>(r)).catch((error) => {
      if (!offlineFailure(error)) throw error;
      return queueMarkUpdate(id, mark, body);
    });
  },
  deleteReaderMark: (id: string, mark: ReaderMark) =>
    fetch(`/api/books/${id}/${mark.kind}s/${mark.id}`, {
      method: "DELETE",
      headers: csrfHeaders(),
    }).then((r) => j<void>(r)).catch((error) => {
      if (!offlineFailure(error)) throw error;
      queueMarkDelete(id, mark);
    }),
  putReaderPreferences: (id: string, preferences: Omit<ReaderPreferences, "preference_version">) =>
    fetch(`/api/books/${id}/preferences`, {
      method: "PUT",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify(preferences),
      keepalive: true,
    }).then((r) => j<ReaderPreferences>(r)),
  putPosition: (
    id: string,
    cfi: string,
    offset: number,
    completedChapter: number,
    positionEpoch: number,
    baseVersion: number,
    clientId: string,
    clientSequence: number,
  ) => {
    const url = `/api/books/${id}/position`;
    const body = {
      cfi,
      offset,
      completed_chapter: completedChapter,
      position_epoch: positionEpoch,
      base_version: baseVersion,
      client_id: clientId,
      client_sequence: clientSequence,
    };
    // keepalive: the flush-on-pagehide report must survive the page teardown
    return fetch(url, {
      method: "PUT",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify(body),
      keepalive: true,
    }).then((r) => j<PutPosition>(r)).catch((error) => {
      if (!offlineFailure(error)) throw error;
      return queuePosition(id, url, body) as PutPosition;
    });
  },
  resetPosition: (id: string, positionEpoch: number) =>
    fetch(`/api/books/${id}/position/reset`, {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ position_epoch: positionEpoch }),
    }).then((r) => j<Position>(r)),
  // bookmark is optional: omitted = the current frontier; set = the scrubber's past point (LIT-15).
  // Either way the server CLAMPS to the high-water — the client never widens its own frontier.
  catchMeUp: (id: string, bookmark?: number) =>
    fetch(`/api/books/${id}/catch-me-up${bookmark != null ? `?bookmark=${bookmark}` : ""}`).then(
      (r) => j<CatchMeUp>(r),
    ),
  askBook: (id: string, question: string, bookmark?: number) =>
    fetch(`/api/books/${id}/ask`, {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ question, ...(bookmark != null ? { bookmark } : {}) }),
    }).then((r) => j<AskAnswer>(r)),
  selectionAction: (
    id: string,
    value: { action: SelectionAction; text: string; atom: number; cfi: string },
  ) => fetch(`/api/books/${id}/selection-action`, {
    method: "POST",
    headers: { "content-type": "application/json", ...csrfHeaders() },
    body: JSON.stringify({
      ...value,
      ...(value.action === "translate" ? { target_language: "English" } : {}),
    }),
  }).then((r) => j<SelectionAssistAnswer>(r)),
  chapterCloseout: (id: string, chapter: number) =>
    fetch(`/api/books/${id}/chapter-closeout`, {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ chapter }),
    }).then((r) => j<ChapterCloseout>(r)),
  // a character's bookmark-clamped card (identity + ties); server 404s a future/unknown entity (LIT-30)
  character: (id: string, entityId: number, bookmark?: number) =>
    fetch(
      `/api/books/${id}/character/${entityId}${bookmark != null ? `?bookmark=${bookmark}` : ""}`,
    ).then((r) => j<Character>(r)),
  ingest: (id: string) => fetch(`/api/books/${id}/ingest`).then((r) => j<IngestStatus>(r)),
  // the character graph as of `bookmark` (the scrubber's point); server clamps to the high-water
  graph: (id: string, bookmark?: number) =>
    fetch(`/api/books/${id}/graph${bookmark != null ? `?bookmark=${bookmark}` : ""}`).then(
      (r) => j<Graph>(r),
    ),
  memoryCorrections: (id: string, bookmark?: number) =>
    fetch(
      `/api/books/${id}/memory-corrections${bookmark != null ? `?bookmark=${bookmark}` : ""}`,
    ).then((r) => j<MemoryCorrections>(r)),
  correctMemory: (
    id: string,
    value: { source_entity_id: number; canonical_name: string; reason: string; bookmark: number },
  ) => fetch(`/api/books/${id}/memory-corrections`, {
    method: "POST",
    headers: { "content-type": "application/json", ...csrfHeaders() },
    body: JSON.stringify(value),
  }).then((r) => j<MemoryCorrections & { correction_id: number | string; target_entity_id: number | string }>(r)),
  // the chapter-by-chapter breakdown (summary + highlights) + visible cast, clamped to `bookmark` (LIT-31)
  notes: (id: string, bookmark?: number) =>
    fetch(`/api/books/${id}/notes${bookmark != null ? `?bookmark=${bookmark}` : ""}`).then(
      (r) => j<Notes>(r),
    ),
  credentials: () => fetch("/api/credentials").then((r) => j<CredentialMetadata[]>(r)),
  createCredential: (provider: Exclude<HostedProvider, "offline">, secret: string) =>
    fetch("/api/credentials", {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ provider, secret }),
    }).then((r) => j<CredentialMetadata>(r)),
  deleteCredential: (id: string) =>
    fetch(`/api/credentials/${id}`, { method: "DELETE", headers: csrfHeaders() }).then(
      (r) => j<void>(r),
    ),
  providerSettings: () =>
    fetch("/api/provider-settings").then((r) => j<ProviderSettingsPayload>(r)),
  putProviderSetting: (capability: ProviderCapability, value: ProviderSettingInput) =>
    fetch(`/api/provider-settings/${capability}`, {
      method: "PUT",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify(value),
    }).then((r) => j<ProviderSetting>(r)),
  validateProviderSetting: (capability: ProviderCapability) =>
    fetch(`/api/provider-settings/${capability}/validate`, {
      method: "POST",
      headers: csrfHeaders(),
    }).then((r) => j<ProviderValidation>(r)),
  logout: async () => {
    try {
      await fetch("/api/auth/logout", { method: "POST", headers: csrfHeaders() }).then(
        (r) => j<void>(r),
      );
    } finally {
      await purgeOfflineTenant();
    }
  },
};
