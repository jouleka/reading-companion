import { useEffect, useState } from "react";

type InstallChoice = { outcome: "accepted" | "dismissed"; platform: string };
type InstallPromptEvent = Event & {
  prompt: () => Promise<void>;
  userChoice: Promise<InstallChoice>;
};

export function InstallApp() {
  const [prompt, setPrompt] = useState<InstallPromptEvent | null>(null);
  const [online, setOnline] = useState(() => navigator.onLine);

  useEffect(() => {
    const offer = (event: Event) => {
      event.preventDefault();
      setPrompt(event as InstallPromptEvent);
    };
    const connected = () => setOnline(true);
    const disconnected = () => setOnline(false);
    globalThis.addEventListener("beforeinstallprompt", offer);
    globalThis.addEventListener("online", connected);
    globalThis.addEventListener("offline", disconnected);
    return () => {
      globalThis.removeEventListener("beforeinstallprompt", offer);
      globalThis.removeEventListener("online", connected);
      globalThis.removeEventListener("offline", disconnected);
    };
  }, []);

  const install = async () => {
    if (!prompt) return;
    await prompt.prompt();
    await prompt.userChoice;
    setPrompt(null);
  };

  return (
    <div className="pwa-status">
      <span className={online ? "online" : "offline"} aria-live="polite">
        {online ? "online" : "offline — changes will sync when connected"}
      </span>
      {prompt && <button type="button" className="plain" onClick={() => { void install(); }}>install app</button>}
    </div>
  );
}
