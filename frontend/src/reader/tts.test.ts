import { describe, expect, test, vi } from "vitest";
import { BrowserTTSController, speechFragment, type FoliateTTS, type TTSState } from "./tts";

const marked = (body: string) =>
  `<speak xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en">${body}</speak>`;

describe("browser read aloud", () => {
  test("converts Foliate SSML to plain speech while retaining word-boundary marks", () => {
    expect(speechFragment(marked('<mark name="0"/>Hello <mark name="1"/>world'))).toEqual({
      text: "Hello world",
      lang: "en",
      marks: [{ name: "0", offset: 0 }, { name: "1", offset: 6 }],
    });
  });

  test("speaks the current passage, synchronizes word selection, pauses, and clears on stop", async () => {
    const host = document.createElement("div");
    host.innerHTML = "<p>Hello world</p>";
    document.body.append(host);
    const doc = document;
    const text = host.querySelector("p")!.firstChild!;
    const range = document.createRange();
    range.setStart(text, 0);
    range.setEnd(text, 5);
    const setMark = vi.fn();
    const tts: FoliateTTS = {
      doc,
      start: vi.fn(() => marked('<mark name="0"/>Hello <mark name="1"/>world')),
      resume: vi.fn(),
      from: vi.fn(() => marked('<mark name="0"/>Hello <mark name="1"/>world')),
      prev: vi.fn(),
      next: vi.fn(),
      setMark,
    };
    let highlight: ((range: Range) => void) | undefined;
    const view = {
      initTTS: vi.fn(async (_granularity, callback) => {
        highlight = callback;
        callback?.(range);
      }),
      tts,
      next: vi.fn(async () => {}),
      renderer: { scrollToAnchor: vi.fn() },
    };
    const utterances: Array<SpeechSynthesisUtterance & {
      onboundary: ((event: SpeechSynthesisEvent) => void) | null;
      onend: ((event: SpeechSynthesisEvent) => void) | null;
      onerror: ((event: SpeechSynthesisErrorEvent) => void) | null;
    }> = [];
    const speech = {
      speak: vi.fn(),
      cancel: vi.fn(),
      pause: vi.fn(),
      resume: vi.fn(),
    };
    const states: TTSState[] = [];
    const controller = new BrowserTTSController(
      speech,
      (spokenText) => {
        const utterance = {
          text: spokenText,
          rate: 1,
          lang: "",
          voice: null,
          onboundary: null,
          onend: null,
          onerror: null,
        } as unknown as typeof utterances[number];
        utterances.push(utterance);
        return utterance;
      },
      () => view,
      (state) => states.push(state),
      () => range,
    );

    await controller.play();
    expect(tts.from).toHaveBeenCalledWith(range);
    expect(view.initTTS).toHaveBeenCalledWith("word", expect.any(Function));
    expect(highlight).toBeTypeOf("function");
    expect(doc.getSelection()?.toString()).toBe("Hello");
    expect(view.renderer.scrollToAnchor).toHaveBeenCalledWith(range, true);
    expect(utterances[0].text).toBe("Hello world");
    expect(speech.speak).toHaveBeenCalledWith(utterances[0]);
    expect(setMark).toHaveBeenCalledWith("0");
    utterances[0].onboundary?.({ charIndex: 6 } as SpeechSynthesisEvent);
    expect(setMark).toHaveBeenLastCalledWith("1");

    controller.pause();
    expect(speech.pause).toHaveBeenCalledOnce();
    expect(states.at(-1)?.mode).toBe("paused");
    await controller.play();
    expect(speech.resume).toHaveBeenCalledOnce();
    controller.stop();
    expect(speech.cancel).toHaveBeenCalled();
    expect(doc.getSelection()?.isCollapsed).toBe(true);
    expect(states.at(-1)).toEqual({ mode: "idle", message: "Read aloud stopped." });
    host.remove();
  });

  test("continues into the next loaded section without replaying the exhausted document", async () => {
    const firstDocument = document.implementation.createHTMLDocument("one");
    const secondDocument = document.implementation.createHTMLDocument("two");
    const first: FoliateTTS = {
      doc: firstDocument,
      start: () => marked("First section."),
      resume: () => undefined,
      from: () => undefined,
      prev: () => undefined,
      next: () => undefined,
      setMark: vi.fn(),
    };
    const second: FoliateTTS = {
      doc: secondDocument,
      start: () => marked("Second section."),
      resume: () => undefined,
      from: () => undefined,
      prev: () => undefined,
      next: () => undefined,
      setMark: vi.fn(),
    };
    const view = {
      initTTS: vi.fn(async () => {}),
      tts: first as FoliateTTS | null,
      next: vi.fn(async () => { view.tts = second; }),
      renderer: {},
    };
    const utterances: Array<{
      text: string;
      rate: number;
      lang: string;
      voice: SpeechSynthesisVoice | null;
      onboundary: ((event: SpeechSynthesisEvent) => void) | null;
      onend: ((event: SpeechSynthesisEvent) => void) | null;
      onerror: ((event: SpeechSynthesisErrorEvent) => void) | null;
    }> = [];
    const controller = new BrowserTTSController(
      { speak: vi.fn(), cancel: vi.fn(), pause: vi.fn(), resume: vi.fn() },
      (text) => {
        const utterance = {
          text, rate: 1, lang: "", voice: null, onboundary: null, onend: null, onerror: null,
        };
        utterances.push(utterance);
        return utterance as unknown as SpeechSynthesisUtterance & typeof utterance;
      },
      () => view,
      () => {},
    );
    await controller.play();
    utterances[0].onend?.({} as SpeechSynthesisEvent);
    await vi.waitFor(() => expect(utterances.map((item) => item.text)).toEqual([
      "First section.", "Second section.",
    ]));
    expect(view.next).toHaveBeenCalledOnce();
    controller.stop();
  });
});
