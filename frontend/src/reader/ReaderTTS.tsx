import { useEffect, useRef, useState } from "react";
import { BrowserTTSController, type TTSMode, type TTSView } from "./tts";

export function ReaderTTS({
  getView,
  getVisibleRange = () => null,
  inactive = false,
  resetKey,
}: {
  getView: () => TTSView | null;
  getVisibleRange?: () => Range | null;
  inactive?: boolean;
  resetKey: string;
}) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<TTSMode>("idle");
  const [message, setMessage] = useState("Read aloud is stopped.");
  const [rate, setRate] = useState(1);
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>([]);
  const [voiceURI, setVoiceURI] = useState("");
  const controllerRef = useRef<BrowserTTSController | null>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const getViewRef = useRef(getView);
  const getRangeRef = useRef(getVisibleRange);
  getViewRef.current = getView;
  getRangeRef.current = getVisibleRange;
  const supported = typeof window !== "undefined"
    && "speechSynthesis" in window
    && "SpeechSynthesisUtterance" in window;

  useEffect(() => {
    if (!supported) return;
    const synthesis = window.speechSynthesis;
    const loadVoices = () => setVoices(synthesis.getVoices());
    loadVoices();
    synthesis.addEventListener("voiceschanged", loadVoices);
    const controller = new BrowserTTSController(
      synthesis,
      (text) => new SpeechSynthesisUtterance(text),
      () => getViewRef.current(),
      (state) => {
        setMode(state.mode);
        setMessage(state.message);
      },
      () => getRangeRef.current(),
    );
    controllerRef.current = controller;
    return () => {
      synthesis.removeEventListener("voiceschanged", loadVoices);
      controller.destroy();
      controllerRef.current = null;
    };
  }, [supported, resetKey]);

  useEffect(() => {
    controllerRef.current?.setRate(rate);
  }, [rate]);

  useEffect(() => {
    const voice = voices.find((candidate) => candidate.voiceURI === voiceURI) ?? null;
    controllerRef.current?.setVoice(voice);
  }, [voiceURI, voices]);

  useEffect(() => {
    if (inactive && mode === "playing") controllerRef.current?.pause();
  }, [inactive, mode]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpen(false);
      toggleRef.current?.focus();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open]);

  const selectedVoice = voices.find((voice) => voice.voiceURI === voiceURI) ?? null;

  return (
    <div className="reader-tts">
      <button
        type="button"
        ref={toggleRef}
        className="plain smallcaps reader-tts-toggle"
        aria-expanded={open}
        onClick={() => setOpen((shown) => !shown)}
      >
        read aloud
      </button>
      {open && (
        <section className="reader-tts-panel" aria-label="Read aloud controls">
          <div className="reader-tts-actions">
            <button
              type="button"
              disabled={!supported || inactive}
              onClick={() => {
                if (mode === "playing") controllerRef.current?.pause();
                else void controllerRef.current?.play();
              }}
            >
              {mode === "playing" ? "Pause" : mode === "paused" ? "Resume" : "Play"}
            </button>
            <button
              type="button"
              disabled={!supported || inactive}
              onClick={() => void controllerRef.current?.previousBlock()}
            >
              Previous passage
            </button>
            <button
              type="button"
              disabled={!supported || inactive}
              onClick={() => void controllerRef.current?.nextBlock()}
            >
              Next passage
            </button>
            <button
              type="button"
              disabled={!supported || mode === "idle"}
              onClick={() => controllerRef.current?.stop()}
            >
              Stop
            </button>
          </div>
          <label>
            <span>Speed</span>
            <select value={rate} onChange={(event) => setRate(Number(event.target.value))}>
              <option value={0.5}>0.5×</option>
              <option value={0.75}>0.75×</option>
              <option value={1}>1×</option>
              <option value={1.25}>1.25×</option>
              <option value={1.5}>1.5×</option>
              <option value={2}>2×</option>
            </select>
          </label>
          <label>
            <span>Voice</span>
            <select value={voiceURI} onChange={(event) => setVoiceURI(event.target.value)}>
              <option value="">Browser default</option>
              {voices.map((voice) => (
                <option value={voice.voiceURI} key={`${voice.voiceURI}:${voice.lang}`}>
                  {voice.name} ({voice.lang}){voice.localService ? " — on device" : ""}
                </option>
              ))}
            </select>
          </label>
          <p className="reader-tts-status" role="status" aria-live="polite">
            {supported ? message : "This browser does not provide a speech service."}
          </p>
          <p className="quiet reader-tts-provider">
            Speech source: {selectedVoice?.localService
              ? "an on-device voice reported by this browser."
              : "the browser or operating system; it may use a platform network service."}
          </p>
          <p className="quiet reader-tts-cost">
            Litlet provider cost: $0.00 — no Litlet AI provider, API tokens, or paid book-text request.
            Platform data use or charges, if any, are controlled by the device or browser provider.
          </p>
        </section>
      )}
    </div>
  );
}
