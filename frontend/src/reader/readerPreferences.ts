export type FontSizePreset = "small" | "book" | "large" | "x-large";
export type LineHeightPreset = "compact" | "comfortable" | "relaxed";
export type MeasurePreset = "narrow" | "balanced" | "wide";
export type ReaderTheme = "paper" | "sepia" | "night" | "system";
export type MarginPreset = "compact" | "balanced" | "generous";
export type TypefacePreset = "publisher" | "serif" | "sans";

export type ReaderPreferences = {
  font_size: FontSizePreset;
  line_height: LineHeightPreset;
  measure: MeasurePreset;
  theme: ReaderTheme;
  margins: MarginPreset;
  typeface: TypefacePreset;
  preference_version: number;
};

export type PreferenceSaveStatus = "saved" | "pending" | "saving" | "error";

export const DEFAULT_READER_PREFERENCES: ReaderPreferences = {
  font_size: "book",
  line_height: "comfortable",
  measure: "balanced",
  theme: "paper",
  margins: "balanced",
  typeface: "publisher",
  preference_version: 0,
};

const ALLOWED = {
  font_size: new Set(["small", "book", "large", "x-large"]),
  line_height: new Set(["compact", "comfortable", "relaxed"]),
  measure: new Set(["narrow", "balanced", "wide"]),
  theme: new Set(["paper", "sepia", "night", "system"]),
  margins: new Set(["compact", "balanced", "generous"]),
  typeface: new Set(["publisher", "serif", "sans"]),
} as const;

export function normalizeReaderPreferences(value: unknown): ReaderPreferences {
  if (!value || typeof value !== "object") return { ...DEFAULT_READER_PREFERENCES };
  const input = value as Record<string, unknown>;
  const normalized = { ...DEFAULT_READER_PREFERENCES } as Record<string, unknown>;
  for (const [key, values] of Object.entries(ALLOWED)) {
    if (typeof input[key] === "string" && values.has(input[key] as never)) normalized[key] = input[key];
  }
  normalized.preference_version =
    Number.isSafeInteger(input.preference_version) && Number(input.preference_version) >= 0
      ? Number(input.preference_version)
      : 0;
  return normalized as ReaderPreferences;
}

const FONT_SIZE: Record<FontSizePreset, string> = {
  small: "100%",
  book: "112%",
  large: "125%",
  "x-large": "140%",
};
const LINE_HEIGHT: Record<LineHeightPreset, string> = {
  compact: "1.4",
  comfortable: "1.6",
  relaxed: "1.8",
};
const TYPEFACE: Record<TypefacePreset, string | null> = {
  publisher: null,
  serif: 'Georgia, "Times New Roman", serif',
  sans: 'system-ui, -apple-system, "Segoe UI", sans-serif',
};
export const READER_THEME_COLORS = {
  paper: { background: "#f6f1e7", text: "#211d17", link: "#7d2a26", scheme: "light" },
  sepia: { background: "#f3e4c4", text: "#34261b", link: "#7a2b22", scheme: "light" },
  night: { background: "#17191c", text: "#e8e4dc", link: "#efb47a", scheme: "dark" },
} as const;

function themeRules(theme: ReaderTheme): string {
  const rules = (name: Exclude<ReaderTheme, "system">) => {
    const colors = READER_THEME_COLORS[name];
    return `
      html { color-scheme: ${colors.scheme}; background: ${colors.background} !important; color: ${colors.text} !important; }
      body { background: ${colors.background} !important; color: ${colors.text} !important; }
      a:link, a:visited { color: ${colors.link} !important; }
    `;
  };
  if (theme !== "system") return rules(theme);
  return `${rules("paper")} @media (prefers-color-scheme: dark) { ${rules("night")} }`;
}

export function buildBookStyles(preferences: ReaderPreferences): string {
  const typeface = TYPEFACE[preferences.typeface];
  return `
    @namespace epub "http://www.idpf.org/2007/ops";
    ${themeRules(preferences.theme)}
    html { font-size: ${FONT_SIZE[preferences.font_size]} !important; }
    ${typeface ? `html, body { font-family: ${typeface} !important; }` : ""}
    p, li, blockquote, dd {
      line-height: ${LINE_HEIGHT[preferences.line_height]} !important;
      hanging-punctuation: allow-end last;
      widows: 2;
      orphans: 2;
    }
    pre { white-space: pre-wrap !important; }
    aside[epub|type~="endnote"], aside[epub|type~="footnote"],
    aside[epub|type~="note"], aside[epub|type~="rearnote"] { display: none; }
  `;
}

export function readerLayoutAttributes(
  preferences: ReaderPreferences,
): Record<"max-inline-size" | "margin" | "gap", string> {
  return {
    "max-inline-size": { narrow: "560px", balanced: "720px", wide: "860px" }[preferences.measure],
    margin: { compact: "24px", balanced: "48px", generous: "72px" }[preferences.margins],
    gap: { compact: "4%", balanced: "7%", generous: "10%" }[preferences.margins],
  };
}

export type PreferenceRenderer = {
  setStyles?: (styles: string) => void;
  setAttribute: (name: string, value: string) => void;
};

export function applyReaderPreferences(
  renderer: PreferenceRenderer,
  preferences: ReaderPreferences,
): void {
  const layout = readerLayoutAttributes(preferences);
  // Foliate explicitly re-renders on max-inline-size. Set gap/margin first so that final render sees
  // the complete geometry rather than requiring a later resize to pick up those two attributes.
  renderer.setAttribute("gap", layout.gap);
  renderer.setAttribute("margin", layout.margin);
  renderer.setAttribute("max-inline-size", layout["max-inline-size"]);
  renderer.setStyles?.(buildBookStyles(preferences));
}

const cacheKey = (bookId: string) => `litlet.reader-preferences:${bookId}`;

export function readCachedReaderPreferences(bookId: string): ReaderPreferences {
  try {
    return normalizeReaderPreferences(JSON.parse(localStorage.getItem(cacheKey(bookId)) ?? "null"));
  } catch {
    return { ...DEFAULT_READER_PREFERENCES };
  }
}

export function cacheReaderPreferences(bookId: string, preferences: ReaderPreferences): void {
  try {
    localStorage.setItem(cacheKey(bookId), JSON.stringify(preferences));
  } catch {
    // The server remains authoritative when storage is disabled or full.
  }
}

/** Serialized debounce for whole-object preference writes; newer UI choices coalesce safely. */
export class ReaderPreferenceSaver {
  private timer: ReturnType<typeof setTimeout> | undefined;
  private pending: ReaderPreferences | null = null;
  private inFlight = false;
  private closed = false;
  private closing = false;

  constructor(
    private readonly put: (preferences: ReaderPreferences) => Promise<void>,
    private readonly delayMs = 350,
    private readonly onStatus: (status: PreferenceSaveStatus) => void = () => {},
  ) {}

  schedule(preferences: ReaderPreferences): void {
    if (this.closed || this.closing) return;
    this.pending = preferences;
    clearTimeout(this.timer);
    this.onStatus("pending");
    this.timer = setTimeout(() => void this.fire(), this.delayMs);
  }

  retry(): void {
    if (!this.pending || this.closed || this.inFlight) return;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => void this.fire(), 0);
  }

  flush(): void {
    if (this.pending && !this.inFlight && !this.closed) void this.fire();
  }

  dispose(): void {
    this.closed = true;
    clearTimeout(this.timer);
    this.timer = undefined;
    this.pending = null;
  }

  /** Finish the in-flight write and one latest queued value, then release the saver. */
  drainAndDispose(): void {
    this.closing = true;
    clearTimeout(this.timer);
    this.timer = undefined;
    if (this.pending && !this.inFlight) void this.fire();
    else if (!this.inFlight) this.closed = true;
  }

  private async fire(): Promise<void> {
    clearTimeout(this.timer);
    this.timer = undefined;
    if (this.closed || this.inFlight || !this.pending) return;
    const value = this.pending;
    this.pending = null;
    this.inFlight = true;
    this.onStatus("saving");
    try {
      await this.put(value);
      if (this.closed) return;
      this.inFlight = false;
      if (this.pending) this.timer = setTimeout(() => void this.fire(), 0);
      else {
        this.onStatus("saved");
        if (this.closing) this.closed = true;
      }
    } catch {
      if (this.closed) return;
      this.inFlight = false;
      if (this.closing) {
        this.pending = null;
        this.closed = true;
        return;
      }
      this.pending ??= value;
      this.onStatus("error");
    }
  }
}
