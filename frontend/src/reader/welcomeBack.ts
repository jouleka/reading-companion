import { useCallback, useEffect, useRef, useState } from "react";

export const WELCOME_BACK_GAP_MS = 4 * 60 * 60 * 1000; // ~4h — a return after a real break, not a blink

export type WelcomeBack = { lastChapter: number };

/** LIT-29 "since you were last here" — client-tracked (localStorage) last-open time + last-seen
 * bookmark. On reopen after a gap beyond the threshold, surface the recap ONCE with a welcome-back
 * framing; any interaction (a page turn = a bookmark change, or an explicit dismiss) steps it out of
 * the way and the flowing view resumes. Reuses the flowing recap — no new backend, a presentation
 * moment only. The gap is measured PER BOOK and re-armed when the book changes. Degrades silently
 * where localStorage is unavailable (private mode): it simply never fires. */
export function useWelcomeBack(bookId: string, bookmark: number | null) {
  const [welcomeBack, setWelcomeBack] = useState<WelcomeBack | null>(null);
  const decidedFor = useRef<string | null>(null); // the bookId we've already run the gap-check for
  const suppressNextWrite = useRef(false); // reset should remain a fresh local visit

  useEffect(() => {
    if (bookmark == null) return; // wait for the resumed position to load
    const key = `rc:lastSeen:${bookId}`;
    if (decidedFor.current !== bookId) {
      decidedFor.current = bookId;
      let prior: { t?: unknown } | null = null;
      try {
        const raw = localStorage.getItem(key);
        if (raw) prior = JSON.parse(raw);
      } catch {
        prior = null;
      }
      if (prior && typeof prior.t === "number" && Date.now() - prior.t > WELCOME_BACK_GAP_MS) {
        setWelcomeBack({ lastChapter: bookmark });
      }
    } else {
      setWelcomeBack(null); // a later bookmark change is an interaction — resume the flowing view
    }
    try {
      if (suppressNextWrite.current) {
        suppressNextWrite.current = false;
        localStorage.removeItem(key);
      } else localStorage.setItem(key, JSON.stringify({ t: Date.now(), bm: bookmark }));
    } catch {
      /* localStorage unavailable — welcome-back simply never fires */
    }
  }, [bookId, bookmark]);

  const dismiss = useCallback(() => setWelcomeBack(null), []);
  const forget = useCallback(() => {
    suppressNextWrite.current = true;
    setWelcomeBack(null);
    try {
      localStorage.removeItem(`rc:lastSeen:${bookId}`);
    } catch {
      // localStorage unavailable
    }
  }, [bookId]);
  return { welcomeBack, dismiss, forget };
}
