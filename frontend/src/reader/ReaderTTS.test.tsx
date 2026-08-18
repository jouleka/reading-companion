import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
import { ReaderTTS } from "./ReaderTTS";
import type { FoliateTTS } from "./tts";

afterEach(() => {
  vi.restoreAllMocks();
  Reflect.deleteProperty(window, "speechSynthesis");
  Reflect.deleteProperty(window, "SpeechSynthesisUtterance");
  Reflect.deleteProperty(globalThis, "SpeechSynthesisUtterance");
});

describe("ReaderTTS", () => {
  test("offers synchronized native controls and states the browser source and zero Litlet cost", async () => {
    const user = userEvent.setup();
    const doc = document.implementation.createHTMLDocument("chapter");
    doc.body.innerHTML = "<p>Hello world</p>";
    const voice = { name: "Local Voice", lang: "en-US", voiceURI: "local", localService: true };
    const speech = {
      getVoices: vi.fn(() => [voice]),
      speak: vi.fn(),
      cancel: vi.fn(),
      pause: vi.fn(),
      resume: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    };
    class FakeUtterance {
      rate = 1;
      lang = "";
      voice: SpeechSynthesisVoice | null = null;
      onboundary = null;
      onend = null;
      onerror = null;
      constructor(public text: string) {}
    }
    Object.defineProperty(window, "speechSynthesis", { configurable: true, value: speech });
    Object.defineProperty(window, "SpeechSynthesisUtterance", {
      configurable: true, value: FakeUtterance,
    });
    Object.defineProperty(globalThis, "SpeechSynthesisUtterance", {
      configurable: true, value: FakeUtterance,
    });
    const tts: FoliateTTS = {
      doc,
      start: () => '<speak xmlns="http://www.w3.org/2001/10/synthesis"><mark name="0"/>Hello world</speak>',
      resume: () => undefined,
      from: () => undefined,
      prev: () => undefined,
      next: () => undefined,
      setMark: vi.fn(),
    };
    const view = {
      initTTS: vi.fn(async () => {}),
      tts,
      next: vi.fn(async () => {}),
      renderer: {},
    };

    const { container } = render(<ReaderTTS getView={() => view} resetKey="book" />);
    await user.click(screen.getByRole("button", { name: "read aloud" }));
    expect(screen.getByRole("region", { name: "Read aloud controls" })).toBeTruthy();
    expect(screen.getByText(/Litlet provider cost: \$0\.00/)).toBeTruthy();
    await user.selectOptions(screen.getByLabelText("Voice"), "local");
    expect(screen.getByText(/on-device voice reported by this browser/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Play" }));
    await waitFor(() => expect(speech.speak).toHaveBeenCalledOnce());
    expect(screen.getByRole("button", { name: "Pause" })).toBeTruthy();
    expect(await axeAA(container)).toHaveNoViolations();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("region", { name: "Read aloud controls" })).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "read aloud" }));
  });

  test("truthfully reports when the browser has no speech service", async () => {
    const user = userEvent.setup();
    render(<ReaderTTS getView={() => null} resetKey="book" />);
    await user.click(screen.getByRole("button", { name: "read aloud" }));
    expect(screen.getByText("This browser does not provide a speech service.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Play" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
