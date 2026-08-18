export type FoliateTTS = {
  doc: Document;
  start: () => string | undefined;
  resume: () => string | undefined;
  from: (range: Range) => string | undefined;
  prev: (paused?: boolean) => string | undefined;
  next: (paused?: boolean) => string | undefined;
  setMark: (mark: string) => void;
};

export type TTSView = {
  initTTS: (
    granularity?: "word" | "sentence",
    highlight?: (range: Range) => void,
  ) => Promise<void>;
  tts: FoliateTTS | null;
  next: () => Promise<void>;
  renderer: { scrollToAnchor?: (range: Range, smooth?: boolean) => void };
};

export type TTSMode = "idle" | "playing" | "paused" | "ended" | "error";
export type TTSState = { mode: TTSMode; message: string };

type SpeechEngine = Pick<SpeechSynthesis, "speak" | "cancel" | "pause" | "resume">;
type Utterance = SpeechSynthesisUtterance & {
  onboundary: ((event: SpeechSynthesisEvent) => void) | null;
  onend: ((event: SpeechSynthesisEvent) => void) | null;
  onerror: ((event: SpeechSynthesisErrorEvent) => void) | null;
};

type SpeechFragment = { text: string; lang: string | null; marks: { name: string; offset: number }[] };

export function speechFragment(ssml: string): SpeechFragment {
  const doc = new DOMParser().parseFromString(ssml, "application/xml");
  if (doc.querySelector("parsererror")) throw new Error("The reader returned invalid speech markup.");
  const root = doc.documentElement;
  let text = "";
  const marks: { name: string; offset: number }[] = [];
  const walk = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      text += node.nodeValue ?? "";
      return;
    }
    if (node instanceof Element) {
      if (node.localName === "mark") {
        const name = node.getAttribute("name");
        if (name != null) marks.push({ name, offset: text.length });
        return;
      }
      if (node.localName === "break") text += " ";
    }
    for (const child of Array.from(node.childNodes)) walk(child);
  };
  walk(root);
  return {
    text,
    lang: root.getAttribute("xml:lang") || root.getAttributeNS(
      "http://www.w3.org/XML/1998/namespace",
      "lang",
    ),
    marks,
  };
}

export class BrowserTTSController {
  private generation = 0;
  private mode: TTSMode = "idle";
  private rate = 1;
  private voice: SpeechSynthesisVoice | null = null;
  private highlightedDocument: Document | null = null;

  constructor(
    private readonly speech: SpeechEngine,
    private readonly createUtterance: (text: string) => Utterance,
    private readonly getView: () => TTSView | null,
    private readonly onState: (state: TTSState) => void,
    private readonly getVisibleRange: () => Range | null = () => null,
  ) {}

  setRate(rate: number) {
    this.rate = Math.max(0.5, Math.min(2, rate));
  }

  setVoice(voice: SpeechSynthesisVoice | null) {
    this.voice = voice;
  }

  async play() {
    if (this.mode === "paused") {
      this.speech.resume();
      this.publish("playing", "Reading aloud.");
      return;
    }
    if (this.mode === "playing") return;
    const view = this.getView();
    if (!view) {
      this.publish("error", "The current page is not ready for read aloud.");
      return;
    }
    const generation = ++this.generation;
    try {
      await this.prepare(view);
      if (generation !== this.generation) return;
      const visibleRange = this.getVisibleRange();
      const ssml = visibleRange
        && visibleRange.startContainer.ownerDocument === view.tts?.doc
        ? view.tts.from(visibleRange)
        : view.tts?.start();
      if (!ssml) {
        this.publish("ended", "There is no readable text on this page.");
        return;
      }
      this.speakFragment(view, ssml, generation);
    } catch {
      this.publish("error", "Read aloud could not start in this browser.");
    }
  }

  pause() {
    if (this.mode !== "playing") return;
    this.speech.pause();
    this.publish("paused", "Read aloud paused.");
  }

  stop(message = "Read aloud stopped.") {
    this.generation += 1;
    this.speech.cancel();
    this.clearHighlight();
    this.publish("idle", message);
  }

  async nextBlock() {
    await this.move("next");
  }

  async previousBlock() {
    await this.move("prev");
  }

  destroy() {
    this.stop();
  }

  private async move(direction: "next" | "prev") {
    const view = this.getView();
    if (!view) return;
    const generation = ++this.generation;
    this.speech.cancel();
    try {
      await this.prepare(view);
      if (generation !== this.generation) return;
      const ssml = view.tts?.[direction](true);
      if (ssml) this.speakFragment(view, ssml, generation);
      else if (direction === "next") await this.advanceSection(view, generation);
      else this.publish("paused", "This is the first readable block on the page.");
    } catch {
      this.publish("error", "Read aloud could not move to that passage.");
    }
  }

  private async prepare(view: TTSView) {
    await view.initTTS("word", (range) => {
      this.clearHighlight();
      const document = range.startContainer.ownerDocument;
      if (!document) return;
      const selection = document.getSelection();
      selection?.removeAllRanges();
      selection?.addRange(range.cloneRange());
      this.highlightedDocument = document;
      view.renderer.scrollToAnchor?.(range, true);
    });
  }

  private speakFragment(view: TTSView, ssml: string, generation: number) {
    const fragment = speechFragment(ssml);
    if (!fragment.text.trim()) {
      void this.continueToNext(view, generation);
      return;
    }
    const utterance = this.createUtterance(fragment.text);
    utterance.rate = this.rate;
    if (fragment.lang) utterance.lang = fragment.lang;
    if (this.voice) utterance.voice = this.voice;
    let markIndex = -1;
    const applyMark = (charIndex: number) => {
      while (
        markIndex + 1 < fragment.marks.length
        && fragment.marks[markIndex + 1].offset <= charIndex
      ) markIndex += 1;
      if (markIndex >= 0) view.tts?.setMark(fragment.marks[markIndex].name);
    };
    applyMark(0);
    utterance.onboundary = (event) => {
      if (generation === this.generation) applyMark(event.charIndex);
    };
    utterance.onend = () => {
      if (generation === this.generation && this.mode === "playing") {
        void this.continueToNext(view, generation);
      }
    };
    utterance.onerror = () => {
      if (generation === this.generation) {
        this.clearHighlight();
        this.publish("error", "The browser speech service stopped unexpectedly.");
      }
    };
    this.publish("playing", "Reading aloud.");
    this.speech.speak(utterance);
  }

  private async continueToNext(view: TTSView, generation: number) {
    const ssml = view.tts?.next();
    if (generation !== this.generation) return;
    if (ssml) {
      this.speakFragment(view, ssml, generation);
      return;
    }
    await this.advanceSection(view, generation);
  }

  private async advanceSection(view: TTSView, generation: number) {
    const previousDocument = view.tts?.doc;
    await view.next();
    await this.prepare(view);
    if (generation !== this.generation) return;
    if (!view.tts || view.tts.doc === previousDocument) {
      this.clearHighlight();
      this.publish("ended", "Read aloud reached the end of the available text.");
      return;
    }
    const ssml = view.tts.start();
    if (ssml) this.speakFragment(view, ssml, generation);
    else {
      this.clearHighlight();
      this.publish("ended", "Read aloud reached the end of the available text.");
    }
  }

  private clearHighlight() {
    this.highlightedDocument?.getSelection()?.removeAllRanges();
    this.highlightedDocument = null;
  }

  private publish(mode: TTSMode, message: string) {
    this.mode = mode;
    this.onState({ mode, message });
  }
}
