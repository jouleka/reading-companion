/** The split-pane reader (LIT-13): foliate-view on the book leaf, the companion on the bound-in
 * leaf. Position flows relocate -> conservative offset (offset.ts) -> jump guard (guard.ts) ->
 * debounced PUT /position (reporter.ts); the companion re-renders from the CLAMPED server responses
 * only. Spoiler discipline: sections map to atoms ONCE against the whole spine, far jumps (the real
 * Karamazov ToC links every chapter) never auto-report — the bookmark is a permanent ratchet — and
 * the pending report is FLUSHED, not dropped, when the reader leaves. */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  type HighlightMark,
  type Manifest,
  type Position,
  type PutPosition,
  type ReaderMark,
  type ReaderMarkAnchor,
  type SelectionAction,
} from "../api";
import { CatchMeUp } from "./CatchMeUp";
import { AskTheBook } from "./AskTheBook";
import { ChapterCloseout } from "./ChapterCloseout";
import { Codex } from "./Codex";
import { Companion } from "./Companion";
import { admitRelocation, arrowPagingBlocked, jumpsAhead } from "./guard";
import { JumpNote } from "./JumpNote";
import { buildSectionAtomMap, normalizeHref, offsetForAtom } from "./offset";
import { nextPositionClientSequence, positionClientId } from "./positionClient";
import { ReaderControls } from "./ReaderControls";
import { ReaderTTS } from "./ReaderTTS";
import { ReaderNavigation } from "./BookNavigation";
import { ReaderMarks, type ReaderSelection } from "./ReaderMarksPanel";
import { SelectionAssistant } from "./SelectionAssistant";
import {
  mapReaderToc,
  tocAtomForHref,
  type EpubTocItem,
  type ReaderSearchExcerpt,
  type ReaderSearchResult,
  type ReaderTocItem,
} from "./readerNavigation";
import {
  applyReaderPreferences,
  cacheReaderPreferences,
  normalizeReaderPreferences,
  readCachedReaderPreferences,
  ReaderPreferenceSaver,
  type PreferenceSaveStatus,
  type ReaderPreferences,
} from "./readerPreferences";
import { PositionReporter, type PositionSyncState } from "./reporter";
import { StartOverNote } from "./StartOverNote";
import { useWelcomeBack } from "./welcomeBack";
import { useCompactReaderShell } from "./mobileShell";
import {
  HIGHLIGHT_COLORS,
  localAnnotation,
  localBookmark,
  localHighlight,
  readLocalReaderMarks,
  visibleReaderMarks,
  writeLocalReaderMarks,
} from "./readerMarks";
import type { FoliateTTS } from "./tts";

type FoliateView = HTMLElement & {
  open: (book: unknown) => Promise<void>;
  goTo: (target: number | string) => Promise<void>;
  select: (target: string) => Promise<void>;
  getCFI: (index: number, range?: Range) => string;
  addAnnotation: (annotation: { value: string; color: string; note?: string }) => Promise<unknown>;
  deleteAnnotation: (annotation: { value: string; color: string; note?: string }) => Promise<unknown>;
  deselect: () => void;
  goToFraction: (f: number) => Promise<void>;
  init: (opts: { lastLocation?: string }) => Promise<void>;
  prev: () => Promise<void>;
  next: () => Promise<void>;
  initTTS: (
    granularity?: "word" | "sentence",
    highlight?: (range: Range) => void,
  ) => Promise<void>;
  tts: FoliateTTS | null;
  search: (options: { query: string; index?: number }) => AsyncGenerator<
    | string
    | {
        cfi?: string;
        excerpt?: ReaderSearchExcerpt;
        progress?: number;
        subitems?: { cfi: string; excerpt: ReaderSearchExcerpt }[];
      }
  >;
  clearSearch: () => void;
  getTOCItemOf: (target: string) => Promise<EpubTocItem | undefined>;
  resolveNavigation: (target: string) => { index: number } | undefined;
  history: {
    back: () => void;
    forward: () => void;
    canGoBack: boolean;
    canGoForward: boolean;
    addEventListener: (name: "index-change", listener: () => void) => void;
    removeEventListener: (name: "index-change", listener: () => void) => void;
  };
  renderer: {
    pages?: number;
    page?: number;
    setStyles?: (styles: string) => void;
    scrollToAnchor?: (range: Range, smooth?: boolean) => void;
  } & HTMLElement;
  book: { sections: { id: string }[]; toc?: EpubTocItem[] | null };
};

export function ReaderPage({ bookId, onBack }: { bookId: string; onBack: () => void }) {
  const holder = useRef<HTMLDivElement>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<FoliateView | null>(null);
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [title, setTitle] = useState("");
  const [pos, setPos] = useState<PutPosition | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [heroOpen, setHeroOpen] = useState(false);
  const [codexOpen, setCodexOpen] = useState(false);
  const [askOpen, setAskOpen] = useState(false);
  const [closeoutOpen, setCloseoutOpen] = useState(false);
  const [selectionAssist, setSelectionAssist] = useState<{
    action: SelectionAction;
    selection: ReaderSelection;
  } | null>(null);
  const [startOverOpen, setStartOverOpen] = useState(false);
  const [startOverBusy, setStartOverBusy] = useState(false);
  const [startOverError, setStartOverError] = useState<string | null>(null);
  const [syncState, setSyncState] = useState<PositionSyncState>("idle");
  const [preferences, setPreferences] = useState<ReaderPreferences>(() =>
    readCachedReaderPreferences(bookId),
  );
  const [preferenceStatus, setPreferenceStatus] = useState<PreferenceSaveStatus>("saved");
  const [toc, setToc] = useState<ReaderTocItem[]>([]);
  const [currentAtom, setCurrentAtom] = useState(0);
  const [historyState, setHistoryState] = useState({ back: false, forward: false });
  const compactShell = useCompactReaderShell();
  const [marks, setMarks] = useState<ReaderMark[]>([]);
  const [readerSelection, setReaderSelection] = useState<ReaderSelection | null>(null);
  const [currentAnchor, setCurrentAnchor] = useState<ReaderMarkAnchor | null>(null);
  const [marksStatus, setMarksStatus] = useState("");
  // a link that leaps past the next chapter awaits explicit confirmation (chapter = 1-based ordinal)
  const [pendingJump, setPendingJump] = useState<{ href: string; chapter: number } | null>(null);
  const reporterRef = useRef<PositionReporter | null>(null);
  const preferenceSaverRef = useRef<ReaderPreferenceSaver | null>(null);
  const preferencesRef = useRef(preferences);
  const sectionAtomRef = useRef<number[]>([]);
  const marksRef = useRef<ReaderMark[]>([]);
  const localMarksRef = useRef<ReaderMark[]>([]);
  const hostedMarksRef = useRef(true);
  const lastAtomRef = useRef(0); // atom of the last accepted report (follows the reader backward)
  const maxAtomRef = useRef(0); // furthest atom ever accepted (returning there is never a jump)
  const allowJumpRef = useRef(false); // set by an explicit confirmation, spent by one relocation
  const manifestBmRef = useRef(0); // bookmark the current manifest labels were clamped at
  const positionEpochRef = useRef(0); // invalidates delayed reports from an earlier reading pass
  const positionVersionRef = useRef(0); // optimistic clock returned by the hosted merge protocol
  const positionReportingPausedRef = useRef(false); // the reset decision freezes old relocations
  const visibleRangeRef = useRef<Range | null>(null); // Foliate's exact current viewport anchor for TTS

  const report = useCallback(
    (cfi: string, offset: number, completedChapter: number) => {
      if (!positionReportingPausedRef.current) {
        reporterRef.current?.schedule(cfi, offset, completedChapter);
      }
    },
    [],
  );

  const changePreferences = useCallback((next: ReaderPreferences) => {
    const normalized = normalizeReaderPreferences(next);
    preferencesRef.current = normalized;
    setPreferences(normalized);
    cacheReaderPreferences(bookId, normalized);
    if (viewRef.current) applyReaderPreferences(viewRef.current.renderer, normalized);
    preferenceSaverRef.current?.schedule(normalized);
  }, [bookId]);

  const publishMarks = useCallback((next: ReaderMark[]) => {
    marksRef.current = next;
    setMarks(next);
  }, []);

  // un-inert the .reader-grid children SYNCHRONOUSLY inside a full-screen leaf's close handler, before
  // it unmounts, so the leaf's focus-restore lands on the (now focusable) opener not <body> (a11y
  // pass-2, WCAG 2.4.3). Execution order — not effect order — is the guarantee; a reactive effect
  // re-runs too late across the ReaderPage->leaf unmount boundary. Shared by the hero and the codex.
  const unInertGrid = useCallback(() => {
    const grid = gridRef.current;
    if (grid) for (const child of Array.from(grid.children)) (child as HTMLElement).inert = false;
  }, []);

  // LIT-29 "since you were last here": on reopen after a ~4h+ gap, auto-surface the story-so-far once
  // with a welcome-back framing; closing it (a page turn also clears it) steps it out of the way.
  const { welcomeBack, dismiss, forget } = useWelcomeBack(bookId, pos?.bookmark ?? null);
  useEffect(() => {
    if (welcomeBack) setHeroOpen(true);
  }, [welcomeBack]);

  useEffect(() => {
    let dead = false;
    let disposeHistory = () => {};
    const clientId = positionClientId();
    const preferenceSaver = new ReaderPreferenceSaver(async (pending) => {
      try {
        const { preference_version: _version, ...body } = pending;
        const saved = normalizeReaderPreferences(await api.putReaderPreferences(bookId, body));
        if (!dead) {
          preferencesRef.current = saved;
          setPreferences(saved);
          cacheReaderPreferences(bookId, saved);
        }
      } catch (error) {
        // Local mode predates the hosted preference route; its constrained cache remains durable.
        if (error instanceof ApiError && error.status === 404) return;
        throw error;
      }
    }, 350, (status) => {
      if (!dead) setPreferenceStatus(status);
    });
    preferenceSaverRef.current = preferenceSaver;
    const normalizePosition = (saved: Position | PutPosition): PutPosition => ({
      ...saved,
      bookmark: saved.bookmark,
      current_chapter: "current_chapter" in saved ? saved.current_chapter : saved.bookmark + 1,
      chapter_progress: "chapter_progress" in saved ? saved.chapter_progress : 0,
      position_epoch: saved.position_epoch,
    });
    const reporter = new PositionReporter(async (cfi, offset, completedChapter) => {
      const requestEpoch = positionEpochRef.current;
      const baseVersion = positionVersionRef.current;
      try {
        const p = await api.putPosition(
          bookId,
          cfi,
          offset,
          completedChapter,
          requestEpoch,
          baseVersion,
          clientId,
          nextPositionClientSequence(),
        );
        if (dead || p.position_epoch !== positionEpochRef.current) return;
        positionVersionRef.current = p.position_version ?? baseVersion + 1;
        setPos((current) => normalizePosition(
          p.queued && current
            ? {
                ...p,
                bookmark: Math.max(current.bookmark, p.bookmark),
                completed_chapter: Math.max(
                  current.completed_chapter ?? current.bookmark,
                  p.completed_chapter ?? p.bookmark,
                ),
                high_water_offset: Math.max(
                  current.high_water_offset ?? 0,
                  p.high_water_offset ?? 0,
                ),
              }
            : p,
        ));
        // labels are clamped server-side to bookmark+1: a newly completed chapter unlocks the
        // next label, so refresh the manifest (structural fields never change). The ref advances
        // only on SUCCESS — a failed refetch retries on the next PUT instead of skipping the unlock.
        if (!p.queued && p.bookmark > manifestBmRef.current) {
          const fresh = await api.manifest(bookId);
          manifestBmRef.current = p.bookmark;
          if (!dead) setManifest(fresh);
          try {
            const freshMarks = hostedMarksRef.current
              ? (await api.readerMarks(bookId)).marks
              : visibleReaderMarks(localMarksRef.current, p.bookmark);
            if (!dead) publishMarks(freshMarks);
          } catch {
            if (!dead) setMarksStatus("marks will refresh when the service reconnects");
          }
        }
        return p.queued ? "queued" : p.applied === false ? "conflict" : "saved";
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) {
          const canonical = await api.position(bookId);
          if (!dead) {
            positionEpochRef.current = canonical.position_epoch;
            positionVersionRef.current = canonical.position_version ?? 0;
            setPos(normalizePosition(canonical));
          }
          return "conflict";
        }
        throw error;
      }
    }, 700, (state) => {
      if (!dead) setSyncState(state);
    }, (error) => !(error instanceof ApiError) || error.status === 429 || error.status >= 500);
    reporterRef.current = reporter;
    const flush = () => {
      reporter.flush();
      preferenceSaver.flush();
    };
    const retry = () => {
      reporter.retry();
      preferenceSaver.retry();
    };
    const reconcileOffline = (event: Event) => {
      const detail = (event as CustomEvent<{ bookId?: string; failed?: boolean }>).detail;
      if (detail?.bookId !== bookId) return;
      void Promise.all([
        api.position(bookId),
        api.readerMarks(bookId),
        api.manifest(bookId),
      ]).then(([fresh, readerMarks, freshManifest]) => {
        if (dead) return;
        positionEpochRef.current = fresh.position_epoch;
        positionVersionRef.current = fresh.position_version ?? positionVersionRef.current;
        setPos(normalizePosition(fresh));
        publishMarks(readerMarks.marks);
        manifestBmRef.current = fresh.bookmark;
        setManifest(freshManifest);
        setMarksStatus(detail.failed ? "an offline change could not be applied" : "offline changes synced");
      }).catch(() => {});
    };
    window.addEventListener("pagehide", flush);
    window.addEventListener("online", retry);
    window.addEventListener("litlet:offline-sync", reconcileOffline);

    (async () => {
      try {
        const [m, books, saved, remotePreferences] = await Promise.all([
          api.manifest(bookId),
          api.books(),
          api.position(bookId),
          api.readerPreferences(bookId).catch((error) => {
            if (error instanceof ApiError && error.status === 404) return preferencesRef.current;
            throw error;
          }),
        ]);
        if (dead) return;
        const loadedPreferences = normalizeReaderPreferences(remotePreferences);
        let loadedMarks: ReaderMark[];
        try {
          loadedMarks = (await api.readerMarks(bookId)).marks;
          hostedMarksRef.current = true;
        } catch (error) {
          if (error instanceof ApiError && error.status === 404) {
            hostedMarksRef.current = false;
            localMarksRef.current = readLocalReaderMarks(bookId);
            loadedMarks = visibleReaderMarks(localMarksRef.current, saved.bookmark);
          } else {
            hostedMarksRef.current = true;
            loadedMarks = [];
            setMarksStatus("marks are temporarily unavailable");
          }
        }
        publishMarks(loadedMarks);
        preferencesRef.current = loadedPreferences;
        setPreferences(loadedPreferences);
        cacheReaderPreferences(bookId, loadedPreferences);
        setManifest(m);
        manifestBmRef.current = saved.bookmark;
        positionEpochRef.current = saved.position_epoch ?? 0;
        positionVersionRef.current = saved.position_version ?? 0;
        setTitle(books.find((b) => b.book_id === bookId)?.title ?? bookId);
        // seed the companion with the SAVED position so a resumed session shows the memory
        // immediately (the ribbon/recap must not claim "nothing read" until the first page turn);
        // the guard seeds the same way: the reader stands in atom[bookmark] (0-based)
        setPos({
          bookmark: saved.bookmark,
          current_chapter: saved.bookmark + 1,
          chapter_progress: 0,
          position_epoch: saved.position_epoch ?? 0,
        });
        lastAtomRef.current = saved.bookmark;
        maxAtomRef.current = saved.bookmark;
        setCurrentAtom(saved.bookmark);

        const [{ makeBook }, { Overlayer }] = await Promise.all([
          import("../vendor/foliate-js/view.js"),
          import("../vendor/foliate-js/overlayer.js"),
        ]);
        const res = await fetch(api.epubUrl(bookId));
        if (!res.ok) throw new Error(`the EPUB could not be fetched (${res.status})`);
        const file = new File([await res.blob()], "book.epub", { type: "application/epub+zip" });
        const book = await makeBook(file);
        if (dead) return;
        const view = document.createElement("foliate-view") as FoliateView;
        holder.current?.replaceChildren(view);

        // one spine-wide mapping — per-relocate href guessing is how front matter could
        // suffix-steal a late atom (see offset.ts)
        const openedBook = book as { sections: { id: string }[]; toc?: EpubTocItem[] | null };
        const bookSections = openedBook.sections;
        const sectionAtom = buildSectionAtomMap(m.atoms, bookSections.map((s) => s.id));
        sectionAtomRef.current = sectionAtom;
        setToc(mapReaderToc(
          openedBook.toc ?? [], bookSections.map((s) => s.id), sectionAtom, m.atoms,
        ));
        const sectionIndexOf = (href: string) => {
          const clean = normalizeHref(href);
          return bookSections.findIndex((s) => normalizeHref(s.id) === clean);
        };
        const annotationFor = (mark: HighlightMark) => ({
          value: mark.anchor.cfi,
          color: HIGHLIGHT_COLORS[mark.color],
        });

        view.addEventListener("draw-annotation", (event) => {
          const detail = (event as CustomEvent<{
            draw: (renderer: unknown, options: { color: string }) => void;
            annotation: { color: string };
          }>).detail;
          detail.draw(Overlayer.highlight, { color: detail.annotation.color });
        });
        view.addEventListener("create-overlay", () => {
          for (const mark of marksRef.current) {
            if (mark.kind === "highlight") void view.addAnnotation(annotationFor(mark));
          }
        });
        view.addEventListener("load", (event) => {
          const { doc, index } = (event as CustomEvent<{ doc: Document; index: number }>).detail;
          doc.addEventListener("pointerup", () => {
            const selected = doc.getSelection();
            if (!selected || selected.rangeCount !== 1 || selected.isCollapsed) return;
            const atom = sectionAtom[index] ?? -1;
            if (atom < 0) return;
            const text = selected.toString().replace(/\s+/g, " ").trim().slice(0, 2000);
            if (!text) return;
            const cfi = view.getCFI(index, selected.getRangeAt(0).cloneRange());
            setReaderSelection({
              anchor: {
                cfi,
                atom: atom + 1,
                quote: { exact: text, prefix: "", suffix: "" },
              },
              text,
            });
          });
        });

        view.addEventListener("relocate", (e) => {
          const d = (e as CustomEvent).detail as {
            cfi?: string;
            range?: Range;
            section?: { current: number };
          };
          const idx = d.section?.current;
          if (d.cfi == null || idx == null) return;
          const atom = sectionAtom[idx] ?? -1;
          if (atom < 0) return; // front/back matter: report nothing, never guess
          visibleRangeRef.current = d.range?.cloneRange() ?? null;
          const verdict = admitRelocation(atom, lastAtomRef.current, maxAtomRef.current, allowJumpRef.current);
          if (!verdict.admit) return; // a far landing without explicit confirmation never reports
          if (verdict.spendToken) allowJumpRef.current = false;
          lastAtomRef.current = atom;
          maxAtomRef.current = Math.max(maxAtomRef.current, atom);
          setCurrentAtom(atom);
          setCurrentAnchor({ cfi: d.cfi, atom: atom + 1 });
          const r = view.renderer;
          const page = r.page ?? 1;
          const pages = r.pages ?? 1;
          const offset = offsetForAtom(m.atoms, atom, page, pages);
          // Entering atom N proves only atoms 0..N-1 complete. Even the last visible page remains
          // inside its atom, so claiming atom+1 here could unlock spoilers before the next section.
          if (offset != null) report(d.cfi, offset, atom);
        });

        // in-book links (the front-matter ToC links EVERY chapter): a leap past the next chapter
        // must be a deliberate act, because the bookmark it would set cannot be lowered again
        view.addEventListener("link", (e) => {
          const le = e as CustomEvent<{ href: string }>;
          const target = sectionAtom[sectionIndexOf(le.detail.href)] ?? -1;
          if (jumpsAhead(target, lastAtomRef.current, maxAtomRef.current)) {
            e.preventDefault();
            setPendingJump({ href: le.detail.href, chapter: target + 1 });
          }
        });

        await view.open(book);
        for (const mark of marksRef.current) {
          if (mark.kind === "highlight") await view.addAnnotation(annotationFor(mark));
        }
        const updateHistory = () => setHistoryState({
          back: view.history.canGoBack,
          forward: view.history.canGoForward,
        });
        view.history.addEventListener("index-change", updateHistory);
        disposeHistory = () => view.history.removeEventListener("index-change", updateHistory);
        // Set both iframe typography and paginator geometry before the first section is initialized,
        // so remote preferences never cause a visible post-render reflow on resume.
        applyReaderPreferences(view.renderer, loadedPreferences);
        // resume at the stored CFI when we have one; otherwise the first section
        if (saved.cfi) await view.init({ lastLocation: saved.cfi });
        else await view.goTo(0);
        viewRef.current = view;
        updateHistory();
      } catch (e) {
        if (!dead) setFatal(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      dead = true;
      window.removeEventListener("pagehide", flush);
      window.removeEventListener("online", retry);
      window.removeEventListener("litlet:offline-sync", reconcileOffline);
      reporter.flush(); // deliver, never drop, the reader's last page-turn
      reporter.dispose();
      preferenceSaver.drainAndDispose();
      preferenceSaverRef.current = null;
      disposeHistory();
      (viewRef.current as { close?: () => void } | null)?.close?.();
      viewRef.current = null; // the keyboard/page-nav handlers must never drive a dead book's view
      visibleRangeRef.current = null;
    };
  }, [bookId, publishMarks, report]);

  // while a full-screen leaf (hero or graph) is open, mark everything behind the scrim inert — so
  // aria-modal is honest: the background is neither tabbable nor exposed to AT (belt to the focus trap)
  const modalOpen = heroOpen || codexOpen || askOpen || closeoutOpen || selectionAssist != null;
  // inert everything behind an open full-screen leaf (hero/graph). The close-time un-inert timing is
  // handled synchronously in each leaf's onClose (below) — a reactive effect re-runs too late for the
  // leaf's focus-restore (a11y pass-2, WCAG 2.4.3).
  useEffect(() => {
    const grid = gridRef.current;
    if (!grid) return;
    for (const child of Array.from(grid.children)) {
      if (!child.classList.contains("hero-scrim")) (child as HTMLElement).inert = modalOpen;
    }
  }, [modalOpen]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (pendingJump || startOverOpen) return; // the confirm dialog owns the keyboard while open
      // don't hijack arrows when focus is in a control/region where arrows have their own meaning
      if (arrowPagingBlocked(e.target)) return;
      if (e.key === "ArrowRight") viewRef.current?.next();
      if (e.key === "ArrowLeft") viewRef.current?.prev();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pendingJump, startOverOpen]);

  const startNewPass = useCallback(async () => {
    if (startOverBusy) return;
    setStartOverBusy(true);
    setStartOverError(null);
    reporterRef.current?.cancel();
    try {
      const reset = await api.resetPosition(bookId, positionEpochRef.current);
      positionEpochRef.current = reset.position_epoch;
      positionVersionRef.current = reset.position_version ?? positionVersionRef.current + 1;
      lastAtomRef.current = 0;
      maxAtomRef.current = 0;
      setCurrentAtom(0);
      setReaderSelection(null);
      manifestBmRef.current = 0;
      allowJumpRef.current = false;
      setPendingJump(null);
      forget();
      setPos({
        bookmark: 0,
        current_chapter: 1,
        chapter_progress: 0,
        position_epoch: reset.position_epoch,
      });
      const followups = await Promise.allSettled([
        api.manifest(bookId).then(setManifest),
        hostedMarksRef.current
          ? api.readerMarks(bookId).then((value) => publishMarks(value.marks))
          : Promise.resolve(publishMarks(visibleReaderMarks(localMarksRef.current, 0))),
        viewRef.current?.goTo(0) ?? Promise.resolve(),
      ]);
      for (const result of followups) {
        if (result.status === "rejected") console.error("new-pass view refresh failed", result.reason);
      }
      positionReportingPausedRef.current = false;
      setSyncState("saved");
      setStartOverOpen(false);
    } catch (error) {
      setStartOverError(error instanceof Error ? error.message : String(error));
    } finally {
      setStartOverBusy(false);
    }
  }, [bookId, forget, publishMarks, startOverBusy]);

  const followJump = async () => {
    if (!pendingJump || !viewRef.current) return;
    allowJumpRef.current = true;
    const { href } = pendingJump;
    setPendingJump(null);
    try {
      await viewRef.current.goTo(href);
    } finally {
      // the landing relocate is dispatched inside the goTo promise chain; anything still unspent
      // here is a leak (a failed goTo must not leave a token for an unrelated far drift)
      allowJumpRef.current = false;
    }
  };

  const navigateToToc = useCallback((item: ReaderTocItem) => {
    if (!item.href || !viewRef.current) return;
    const target = item.atom ?? -1;
    if (jumpsAhead(target, lastAtomRef.current, maxAtomRef.current)) {
      setPendingJump({ href: item.href, chapter: target + 1 });
      return;
    }
    void viewRef.current.goTo(item.href);
  }, []);

  const searchReadPages = useCallback(async (query: string): Promise<ReaderSearchResult[]> => {
    const view = viewRef.current;
    if (!view) return [];
    const completed = pos?.bookmark ?? 0;
    const results: ReaderSearchResult[] = [];
    const include = async (candidate: { cfi: string; excerpt: ReaderSearchExcerpt }) => {
      const tocItem = await view.getTOCItemOf(candidate.cfi);
      const section = view.resolveNavigation(candidate.cfi)?.index ?? -1;
      const tocAtom = tocAtomForHref(toc, tocItem?.href);
      const atom = tocAtom ?? (manifest?.mode === "anchor-driven"
        ? -1
        : (sectionAtomRef.current[section] ?? -1));
      if (atom >= 0 && atom < completed && results.length < 100) {
        results.push({ ...candidate, atom });
      }
    };
    try {
      for await (const result of view.search({ query })) {
        if (typeof result === "string") continue;
        if (result.cfi && result.excerpt) await include(result as {
          cfi: string; excerpt: ReaderSearchExcerpt;
        });
        for (const candidate of result.subitems ?? []) await include(candidate);
        if (results.length >= 100) break;
      }
    } finally {
      // The engine draws all raw matches while scanning. Clear them so future-section matches that
      // were filtered from the result list can never remain as visible annotations.
      view.clearSearch();
    }
    return results;
  }, [manifest?.mode, pos?.bookmark, toc]);

  const appendReaderMark = useCallback((mark: ReaderMark) => {
    if (!hostedMarksRef.current) {
      localMarksRef.current = [...localMarksRef.current, mark];
      writeLocalReaderMarks(bookId, localMarksRef.current);
    }
    publishMarks([...marksRef.current, mark]);
  }, [bookId, publishMarks]);

  const clearReaderSelection = useCallback(() => {
    viewRef.current?.deselect();
    setReaderSelection(null);
  }, []);

  const saveHighlight = useCallback(async (color: HighlightMark["color"]) => {
    if (!readerSelection) return;
    setMarksStatus("saving highlight…");
    try {
      const mark = hostedMarksRef.current
        ? await api.createHighlight(bookId, {
            anchor: readerSelection.anchor,
            color,
            selected_text: readerSelection.text,
          })
        : localHighlight(readerSelection.anchor, readerSelection.text, color);
      appendReaderMark(mark);
      await viewRef.current?.addAnnotation({
        value: mark.anchor.cfi,
        color: HIGHLIGHT_COLORS[mark.color],
      });
      clearReaderSelection();
      setMarksStatus(mark.pending ? "highlight queued for sync" : "highlight saved");
    } catch {
      setMarksStatus("highlight could not be saved");
    }
  }, [appendReaderMark, bookId, clearReaderSelection, readerSelection]);

  const saveAnnotation = useCallback(async (body: string) => {
    if (!readerSelection) return;
    setMarksStatus("saving note…");
    try {
      const mark = hostedMarksRef.current
        ? await api.createAnnotation(bookId, { anchor: readerSelection.anchor, body })
        : localAnnotation(readerSelection.anchor, body);
      appendReaderMark(mark);
      clearReaderSelection();
      setMarksStatus(mark.pending ? "note queued for sync" : "note saved");
    } catch {
      setMarksStatus("note could not be saved");
    }
  }, [appendReaderMark, bookId, clearReaderSelection, readerSelection]);

  const saveBookmark = useCallback(async (label: string) => {
    if (!currentAnchor) return;
    setMarksStatus("saving bookmark…");
    try {
      const mark = hostedMarksRef.current
        ? await api.createBookmark(bookId, {
            anchor: currentAnchor,
            ...(label ? { label } : {}),
          })
        : localBookmark(currentAnchor, label);
      appendReaderMark(mark);
      setMarksStatus(mark.pending ? "bookmark queued for sync" : "bookmark saved");
    } catch {
      setMarksStatus("bookmark could not be saved");
    }
  }, [appendReaderMark, bookId, currentAnchor]);

  const deleteReaderMark = useCallback(async (mark: ReaderMark) => {
    setMarksStatus(`deleting ${mark.kind}…`);
    try {
      if (hostedMarksRef.current) await api.deleteReaderMark(bookId, mark);
      else {
        localMarksRef.current = localMarksRef.current.filter((item) => item.id !== mark.id);
        writeLocalReaderMarks(bookId, localMarksRef.current);
      }
      publishMarks(marksRef.current.filter((item) => item.id !== mark.id));
      if (mark.kind === "highlight") {
        await viewRef.current?.deleteAnnotation({
          value: mark.anchor.cfi,
          color: HIGHLIGHT_COLORS[mark.color],
        });
      }
      setMarksStatus(`${mark.kind} deleted`);
    } catch {
      setMarksStatus(`${mark.kind} could not be deleted`);
    }
  }, [bookId, publishMarks]);

  const chapterLabel =
    pos && manifest
      ? manifest.atoms[Math.min(pos.current_chapter, manifest.atoms.length) - 1]?.title || ""
      : "";

  return (
    <div className="reader-grid" ref={gridRef} data-reader-theme={preferences.theme}>
      <a className="skip-link" href="#main">
        skip to the book
      </a>
      <main id="main" className="book-pane" tabIndex={-1}>
        <header className="running-head">
          <button className="plain smallcaps" onClick={onBack}>
            ‹ the shelf
          </button>
          <h1 className="title">{title}</h1>
          <span className="chapter">{chapterLabel}</span>
          <ReaderControls
            preferences={preferences}
            onChange={changePreferences}
            status={preferenceStatus}
          />
          <ReaderTTS
            getView={() => viewRef.current}
            getVisibleRange={() => visibleRangeRef.current}
            inactive={modalOpen || startOverOpen}
            resetKey={bookId}
          />
          <ReaderNavigation
            toc={toc}
            atoms={manifest?.atoms ?? []}
            currentAtom={currentAtom}
            canGoBack={historyState.back}
            canGoForward={historyState.forward}
            searchableChapters={pos?.bookmark ?? 0}
            onBack={() => viewRef.current?.history.back()}
            onForward={() => viewRef.current?.history.forward()}
            onNavigate={navigateToToc}
            onSearch={searchReadPages}
            onSearchNavigate={(result) => { void viewRef.current?.select(result.cfi); }}
          />
          <ReaderMarks
            marks={marks}
            selection={readerSelection}
            currentAnchor={currentAnchor}
            exportUrl={hostedMarksRef.current
              ? api.readerMarksExportUrl(bookId)
              : `data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify({
                  format: "litlet-reader-marks", version: 1, book_id: bookId,
                  as_of_chapter: pos?.bookmark ?? 0, marks,
                }))}`}
            status={marksStatus}
            onAssist={(action) => {
              if (readerSelection) setSelectionAssist({ action, selection: readerSelection });
            }}
            onHighlight={(color) => { void saveHighlight(color); }}
            onAnnotate={(body) => { void saveAnnotation(body); }}
            onBookmark={(label) => { void saveBookmark(label); }}
            onNavigate={(mark) => { void viewRef.current?.goTo(mark.anchor.cfi); }}
            onDelete={(mark) => { void deleteReaderMark(mark); }}
            onClearSelection={clearReaderSelection}
          />
          <span className={`sync-state ${syncState}`} role="status" aria-live="polite">
            {syncState === "saving" || syncState === "pending"
              ? "saving…"
              : syncState === "queued"
                ? "waiting to sync"
                : syncState === "conflict"
                  ? "newer progress kept"
                  : syncState === "error"
                    ? "sync needs attention"
                    : "saved"}
          </span>
          {pos && pos.bookmark > 0 && (
            <button
              className="plain smallcaps"
              onClick={() => {
                positionReportingPausedRef.current = true;
                setStartOverError(null);
                setStartOverOpen(true);
              }}
            >
              start over
            </button>
          )}
        </header>
        {fatal ? (
          <p style={{ padding: 40 }} className="quiet" role="alert">
            The book could not be opened: {fatal}
          </p>
        ) : (
          <div className="book-view" ref={holder} />
        )}
        {pendingJump && (
          <JumpNote
            chapter={pendingJump.chapter}
            onFollow={followJump}
            onStay={() => setPendingJump(null)}
          />
        )}
        {startOverOpen && (
          <StartOverNote
            busy={startOverBusy}
            error={startOverError}
            onConfirm={startNewPass}
            onCancel={() => {
              positionReportingPausedRef.current = false;
              setStartOverError(null);
              setStartOverOpen(false);
            }}
          />
        )}
        <button
          className="page-nav prev"
          aria-label="Previous page"
          disabled={startOverOpen}
          onClick={() => viewRef.current?.prev()}
        >
          ❮
        </button>
        <button
          className="page-nav next"
          aria-label="Next page"
          disabled={startOverOpen}
          onClick={() => viewRef.current?.next()}
        >
          ❯
        </button>
      </main>
      <Companion
        bookId={bookId}
        bookmark={pos?.bookmark ?? null}
        totalAtoms={manifest?.atoms.length ?? 0}
        bookType={manifest?.book_profile?.book_type ?? "novel"}
        onOpenHero={() => setHeroOpen(true)}
        onOpenCodex={() => setCodexOpen(true)}
        onOpenAsk={() => setAskOpen(true)}
        onOpenCloseout={() => setCloseoutOpen(true)}
        compact={compactShell}
      />
      {heroOpen && (
        <CatchMeUp
          bookId={bookId}
          bookmark={pos?.bookmark ?? 0}
          totalAtoms={manifest?.atoms.length ?? 0}
          bookType={manifest?.book_profile?.book_type ?? "novel"}
          welcomeBackChapter={welcomeBack?.lastChapter ?? null}
          onClose={() => {
            unInertGrid();          // synchronous un-inert so the hero's focus-restore reaches the opener
            setHeroOpen(false);
            dismiss();
          }}
        />
      )}
      {codexOpen && (
        <Codex
          bookId={bookId}
          bookmark={pos?.bookmark ?? 0}
          bookType={manifest?.book_profile?.book_type ?? "novel"}
          onClose={() => {
            // synchronous un-inert BEFORE the codex unmounts, so its focus-restore reaches the
            // opener, not <body> — the routed LIT-30 finding, closed here (WCAG 2.4.3)
            unInertGrid();
            setCodexOpen(false);
          }}
        />
      )}
      {askOpen && (
        <AskTheBook
          bookId={bookId}
          bookmark={pos?.bookmark ?? 0}
          onNavigate={(citation) => {
            unInertGrid();
            setAskOpen(false);
            void viewRef.current?.goTo(citation.href);
          }}
          onClose={() => {
            unInertGrid();
            setAskOpen(false);
          }}
        />
      )}
      {selectionAssist && (
        <SelectionAssistant
          bookId={bookId}
          action={selectionAssist.action}
          selection={selectionAssist.selection}
          onNavigate={(cfi) => {
            unInertGrid();
            setSelectionAssist(null);
            void viewRef.current?.select(cfi);
          }}
          onClose={() => {
            unInertGrid();
            setSelectionAssist(null);
          }}
        />
      )}
      {closeoutOpen && (
        <ChapterCloseout
          bookId={bookId}
          chapter={pos?.bookmark ?? 0}
          onNavigate={(citation) => {
            unInertGrid();
            setCloseoutOpen(false);
            void viewRef.current?.goTo(citation.href);
          }}
          onClose={() => {
            unInertGrid();
            setCloseoutOpen(false);
          }}
        />
      )}
    </div>
  );
}
