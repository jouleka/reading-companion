import { useEffect, useRef, useState } from "react";
import { Icon } from "../ui/Icon";
import { Button } from "../ui/Button";
import { useReaderPopover } from "../ui/useReaderPopover";
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
  const { open, closing, id, triggerRef, panelRef, toggle, close } =
    useReaderPopover();
  const [mode, setMode] = useState<TTSMode>("idle");
  const [message, setMessage] = useState("Read aloud is stopped.");
  const [rate, setRate] = useState(1);
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>([]);
  const [voiceURI, setVoiceURI] = useState("");
  const controllerRef = useRef<BrowserTTSController | null>(null);
  const getViewRef = useRef(getView);
  const getRangeRef = useRef(getVisibleRange);
  getViewRef.current = getView;
  getRangeRef.current = getVisibleRange;
  const supported =
    typeof window !== "undefined" &&
    "speechSynthesis" in window &&
    "SpeechSynthesisUtterance" in window;

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
    const voice =
      voices.find((candidate) => candidate.voiceURI === voiceURI) ?? null;
    controllerRef.current?.setVoice(voice);
  }, [voiceURI, voices]);

  useEffect(() => {
    if (inactive && mode === "playing") controllerRef.current?.pause();
  }, [inactive, mode]);

  const selectedVoice =
    voices.find((voice) => voice.voiceURI === voiceURI) ?? null;

  return (
    <div className="reader-tts">
      <button
        type="button"
        ref={triggerRef}
        className="plain reader-tool reader-tts-toggle"
        aria-label="read aloud"
        aria-expanded={open && !closing}
        aria-controls={id}
        onClick={toggle}
      >
        <Icon name="headphones" />
        <span>Listen</span>
        {mode === "playing" && (
          <span className="reader-tool-active" aria-hidden="true" />
        )}
      </button>
      {open && (
        <section
          id={id}
          ref={panelRef}
          tabIndex={-1}
          className="reader-popover reader-tts-panel"
          data-closing={closing || undefined}
          aria-hidden={closing || undefined}
          aria-label="Read aloud controls"
        >
          <div className="reader-panel-heading">
            <h2>Read aloud</h2>
            <Button
              variant="ghost"
              className="icon-button"
              aria-label="Close read aloud controls"
              onClick={() => close()}
            >
              <Icon name="close" />
            </Button>
          </div>
          <div className="reader-tts-actions">
            <Button
              variant="primary"
              className="reader-tts-play"
              disabled={!supported || inactive}
              onClick={() => {
                if (mode === "playing") controllerRef.current?.pause();
                else void controllerRef.current?.play();
              }}
            >
              {mode === "playing"
                ? "Pause"
                : mode === "paused"
                  ? "Resume"
                  : "Play"}
            </Button>
            <Button
              disabled={!supported || inactive}
              onClick={() => void controllerRef.current?.previousBlock()}
            >
              <Icon name="arrowLeft" size={16} /> <span>Previous passage</span>
            </Button>
            <Button
              disabled={!supported || inactive}
              onClick={() => void controllerRef.current?.nextBlock()}
            >
              <span>Next passage</span> <Icon name="arrowRight" size={16} />
            </Button>
            <Button
              variant="ghost"
              disabled={!supported || mode === "idle"}
              onClick={() => controllerRef.current?.stop()}
            >
              Stop
            </Button>
          </div>
          <label>
            <span>Speed</span>
            <select
              value={rate}
              onChange={(event) => setRate(Number(event.target.value))}
            >
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
            <select
              value={voiceURI}
              onChange={(event) => setVoiceURI(event.target.value)}
            >
              <option value="">Browser default</option>
              {voices.map((voice) => (
                <option
                  value={voice.voiceURI}
                  key={`${voice.voiceURI}:${voice.lang}`}
                >
                  {voice.name} ({voice.lang})
                  {voice.localService ? " — on device" : ""}
                </option>
              ))}
            </select>
          </label>
          <p className="reader-tts-status" role="status" aria-live="polite">
            {supported
              ? message
              : "This browser does not provide a speech service."}
          </p>
          <details className="reader-tts-details">
            <summary>Voice source &amp; costs</summary>
            <p className="quiet reader-tts-provider">
              Speech source:{" "}
              {selectedVoice?.localService
                ? "an on-device voice reported by this browser."
                : "the browser or operating system; it may use a platform network service."}
            </p>
            <p className="quiet reader-tts-cost">
              Litlet provider cost: $0.00 — no Litlet AI provider, API tokens,
              or paid book-text request. Platform data use or charges, if any,
              are controlled by the device or browser provider.
            </p>
          </details>
        </section>
      )}
    </div>
  );
}
