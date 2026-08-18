import { afterEach, describe, expect, test, vi } from "vitest";
import {
  DEFAULT_READER_PREFERENCES,
  buildBookStyles,
  normalizeReaderPreferences,
  readerLayoutAttributes,
  READER_THEME_COLORS,
  ReaderPreferenceSaver,
} from "./readerPreferences";

afterEach(() => vi.useRealTimers());

describe("reader typography presets", () => {
  const luminance = (hex: string) => {
    const channels = hex.match(/[0-9a-f]{2}/gi)!.map((value) => parseInt(value, 16) / 255)
      .map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  };
  const contrast = (a: string, b: string) => {
    const [lighter, darker] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (lighter + 0.05) / (darker + 0.05);
  };

  test("defaults are a restrained professional book setting", () => {
    expect(DEFAULT_READER_PREFERENCES).toEqual({
      font_size: "book",
      line_height: "comfortable",
      measure: "balanced",
      theme: "paper",
      margins: "balanced",
      typeface: "publisher",
      preference_version: 0,
    });
  });

  test("rejects unknown cached values instead of injecting arbitrary CSS", () => {
    expect(normalizeReaderPreferences({
      ...DEFAULT_READER_PREFERENCES,
      theme: "url(javascript:bad)",
      font_size: 9000,
    })).toEqual(DEFAULT_READER_PREFERENCES);
  });

  test("maps presets to bounded book CSS and paginator attributes", () => {
    const preferences = {
      ...DEFAULT_READER_PREFERENCES,
      font_size: "large" as const,
      line_height: "relaxed" as const,
      typeface: "sans" as const,
      measure: "narrow" as const,
      margins: "generous" as const,
      theme: "night" as const,
    };
    const css = buildBookStyles(preferences);
    expect(css).toContain("font-size: 125%");
    expect(css).toContain("line-height: 1.8");
    expect(css).toContain("system-ui");
    expect(css).toContain("#17191c");
    expect(css).not.toContain("javascript");
    expect(readerLayoutAttributes(preferences)).toEqual({
      "max-inline-size": "560px",
      margin: "72px",
      gap: "10%",
    });
  });

  test("every fixed theme clears WCAG AA for body text and links", () => {
    for (const colors of Object.values(READER_THEME_COLORS)) {
      expect(contrast(colors.text, colors.background)).toBeGreaterThanOrEqual(4.5);
      expect(contrast(colors.link, colors.background)).toBeGreaterThanOrEqual(4.5);
    }
  });

  test("serializes rapid whole-object saves and retains a failed save for retry", async () => {
    vi.useFakeTimers();
    const saved: string[] = [];
    let fail = true;
    const statuses: string[] = [];
    const saver = new ReaderPreferenceSaver(async (preferences) => {
      if (fail) {
        fail = false;
        throw new TypeError("offline");
      }
      saved.push(preferences.theme);
    }, 20, (status) => statuses.push(status));
    saver.schedule({ ...DEFAULT_READER_PREFERENCES, theme: "sepia" });
    saver.schedule({ ...DEFAULT_READER_PREFERENCES, theme: "night" });
    await vi.advanceTimersByTimeAsync(20);
    expect(statuses.at(-1)).toBe("error");
    saver.retry();
    await vi.advanceTimersByTimeAsync(0);
    expect(saved).toEqual(["night"]);
    expect(statuses.at(-1)).toBe("saved");
  });
});
