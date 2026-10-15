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
import {
  buildSectionAtomMap,
  finalPageOffset,
  normalizeHref,
  offsetForAtom,
} from "./offset";
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
  bindReaderMotion,
  cacheReaderPreferences,
  normalizeReaderPreferences,
  readCachedReaderPreferences,
  ReaderPreferenceSaver,
  type PreferenceSaveStatus,
  type ReaderPreferences,
} from "./readerPreferences";
import { PositionReporter, type PendingPosition, type PositionSyncState } from "./reporter";
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
import { Icon } from "../ui/Icon";
import { Button } from "../ui/Button";
import { presentationFor } from "./bookProfile";
import { activeOfflineOwner, flushOfflineMutations, queuePosition } from "../pwa/offline";

type FoliateView = HTMLElement & {
  open: (book: unknown) => Promise<void>;
  goTo: (target: number | string) => Promise<void>;
  select: (target: string) => Promise<void>;
  getCFI: (index: number, range?: Range) => string;
  addAnnotation: (annotation: {
    value: string;
    color: string;
    note?: string;
  }) => Promise<unknown>;
  deleteAnnotation: (annotation: {
    value: string;
    color: string;
    note?: string;
  }) => Promise<unknown>;
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
  search: (options: {
    query: string;
    index?: number;
    draw?: () => SVGElement;
  }) => AsyncGenerator<
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

export function ReaderPage({
  bookId,
  onBack,
}: {
  bookId: string;
  onBack: () => void;
}) {
  const holder = useRef<HTMLDivElement>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<FoliateView | null>(null);
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [title, setTitle] = useState("");
  const [pos, setPos] = useState<PutPosition | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [readerReady, setReaderReady] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [focusMode, setFocusMode] = useState(false);
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
  const [preferenceStatus, setPreferenceStatus] =
    useState<PreferenceSaveStatus>("saved");
  const [toc, setToc] = useState<ReaderTocItem[]>([]);
  const [currentAtom, setCurrentAtom] = useState(0);
  const [finishCandidate, setFinishCandidate] = useState<{
    cfi: string;
    offset: number;
  } | null>(null);
  const [historyState, setHistoryState] = useState({
    back: false,
    forward: false,
  });
  const compactShell = useCompactReaderShell();
  const [marks, setMarks] = useState<ReaderMark[]>([]);
  const [readerSelection, setReaderSelection] =
    useState<ReaderSelection | null>(null);
  const [currentAnchor, setCurrentAnchor] = useState<ReaderMarkAnchor | null>(
    null,
  );
  const [marksStatus, setMarksStatus] = useState("");
  // a link that leaps past the next chapter awaits explicit confirmation (chapter = 1-based ordinal)
  const [pendingJump, setPendingJump] = useState<{
    href: string;
    chapter: number;
  } | null>(null);
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
        reporterRef.current?.schedule(cfi, offset, completedChapter, positionEpochRef.current);
      }
    },
    [],
  );

  const changePreferences = useCallback(
    (next: ReaderPreferences) => {
      const normalized = normalizeReaderPreferences(next);
      preferencesRef.current = normalized;
      setPreferences(normalized);
      cacheReaderPreferences(bookId, normalized);
      if (viewRef.current)
        applyReaderPreferences(viewRef.current.renderer, normalized);
      preferenceSaverRef.current?.schedule(normalized);
    },
    [bookId],
  );

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
    if (grid)
      for (const child of Array.from(grid.children))
        (child as HTMLElement).inert = false;
  }, []);

  // LIT-29 "since you were last here": on reopen after a ~4h+ gap, auto-surface the story-so-far once
  // with a welcome-back framing; closing it (a page turn also clears it) steps it out of the way.
  const { welcomeBack, dismiss, forget } = useWelcomeBack(
    bookId,
    pos?.bookmark ?? null,
  );
  useEffect(() => {
    if (welcomeBack) setHeroOpen(true);
  }, [welcomeBack]);

  useEffect(() => {
    let dead = false;
    const owner = activeOfflineOwner();
    setReaderReady(false);
    setFinishCandidate(null);
    setFatal(null);
    let disposeHistory = () => {};
    let disposeMotion = () => {};
    const download = new AbortController();
    let ownedView: FoliateView | null = null;
    let ownedBook: { destroy?: () => void } | null = null;
    let initializing = true;
    const releaseBook = () => {
      disposeHistory();
      disposeMotion();
      // A partially initialized paginator has no current document yet. Its destroy method may
      // reject that state; removal and EPUB object-URL cleanup must still happen.
      try {
        (ownedView as { close?: () => void } | null)?.close?.();
      } catch {
        /* teardown only */
      }
      ownedView?.remove();
      ownedView = null;
      ownedBook?.destroy?.();
      ownedBook = null;
    };
    const clientId = positionClientId();
    const preferenceSaver = new ReaderPreferenceSaver(
      async (pending) => {
        try {
          const { preference_version: _version, ...body } = pending;
          const saved = normalizeReaderPreferences(
            await api.putReaderPreferences(bookId, body),
          );
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
      },
      350,
      (status) => {
        if (!dead) setPreferenceStatus(status);
      },
    );
    preferenceSaverRef.current = preferenceSaver;
    const normalizePosition = (saved: Position | PutPosition): PutPosition => ({
      ...saved,
      bookmark: saved.bookmark,
      current_chapter:
        "current_chapter" in saved ? saved.current_chapter : saved.bookmark + 1,
      chapter_progress:
        "chapter_progress" in saved ? saved.chapter_progress : 0,
      position_epoch: saved.position_epoch,
    });
    const reporter = new PositionReporter(
      async (cfi, offset, completedChapter, requestEpoch) => {
        if (activeOfflineOwner() !== owner || requestEpoch !== positionEpochRef.current)
          return "conflict";
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
          if (dead || activeOfflineOwner() !== owner || p.position_epoch !== positionEpochRef.current) return;
          positionVersionRef.current = p.position_version ?? baseVersion + 1;
          setPos((current) =>
            normalizePosition(
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
            ),
          );
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
              if (!dead)
                setMarksStatus(
                  "marks will refresh when the service reconnects",
                );
            }
          }
          return p.queued
            ? "queued"
            : p.applied === false
              ? "conflict"
              : "saved";
        } catch (error) {
          if (error instanceof ApiError && error.status === 409) {
            if (dead || activeOfflineOwner() !== owner) return "conflict";
            const canonical = await api.position(bookId);
            if (!dead && activeOfflineOwner() === owner) {
              if (canonical.position_epoch !== requestEpoch) {
                reporter.cancel();
                // A stale renderer belongs to the old pass. Reopen from canonical position before
                // permitting a new epoch, rather than relabeling its queued old-page reports.
                setLoadAttempt((attempt) => attempt + 1);
              }
              positionEpochRef.current = canonical.position_epoch;
              positionVersionRef.current = canonical.position_version ?? 0;
              setPos(normalizePosition(canonical));
            }
            return "conflict";
          }
          throw error;
        }
      },
      700,
      (state) => {
        if (!dead) setSyncState(state);
      },
      (error) =>
        !(error instanceof ApiError) ||
        error.status === 429 ||
        error.status >= 500,
    );
    reporterRef.current = reporter;
    const retainPosition = (pending: Readonly<PendingPosition>) => {
      if (activeOfflineOwner() !== owner || pending.positionEpoch !== positionEpochRef.current) return;
      queuePosition(bookId, `/api/books/${bookId}/position`, {
        cfi: pending.cfi,
        offset: pending.offset,
        completed_chapter: pending.completedChapter,
        position_epoch: pending.positionEpoch,
        base_version: positionVersionRef.current,
        client_id: clientId,
        client_sequence: nextPositionClientSequence(),
      });
    };
    const flush = () => {
      reporter.retain(retainPosition);
      reporter.flush();
      preferenceSaver.flush();
    };
    const retry = () => {
      reporter.retry();
      preferenceSaver.retry();
    };
    const reconcileOffline = (event: Event) => {
      const detail = (
        event as CustomEvent<{ bookId?: string; failed?: boolean }>
      ).detail;
      if (detail?.bookId !== bookId) return;
      void Promise.all([
        api.position(bookId),
        api.readerMarks(bookId),
        api.manifest(bookId),
      ])
        .then(([fresh, readerMarks, freshManifest]) => {
          if (dead || activeOfflineOwner() !== owner) return;
          if (fresh.position_epoch !== positionEpochRef.current) {
            reporter.cancel();
            setLoadAttempt((attempt) => attempt + 1);
          }
          positionEpochRef.current = fresh.position_epoch;
          positionVersionRef.current =
            fresh.position_version ?? positionVersionRef.current;
          setPos(normalizePosition(fresh));
          publishMarks(readerMarks.marks);
          manifestBmRef.current = fresh.bookmark;
          setManifest(freshManifest);
          setMarksStatus(
            detail.failed
              ? "an offline change could not be applied"
              : "offline changes synced",
          );
        })
        .catch(() => {});
    };
    window.addEventListener("pagehide", flush);
    window.addEventListener("online", retry);
    window.addEventListener("litlet:offline-sync", reconcileOffline);

    (async () => {
      try {
        const [
          loadedManifest,
          books,
          saved,
          remotePreferences,
          remoteMarks,
          [{ makeBook }, { Overlayer }],
          file,
        ] = await Promise.all([
          api.manifest(bookId),
          api.books(),
          api.position(bookId),
          api.readerPreferences(bookId).catch((error) => {
            if (error instanceof ApiError && error.status === 404)
              return preferencesRef.current;
            throw error;
          }),
          api.readerMarks(bookId).then(
            (value) => ({ status: "fulfilled" as const, value }),
            (reason: unknown) => ({ status: "rejected" as const, reason }),
          ),
          Promise.all([
            import("../vendor/foliate-js/view.js"),
            import("../vendor/foliate-js/overlayer.js"),
          ]),
          fetch(api.epubUrl(bookId), { signal: download.signal }).then(
            async (response) => {
              if (!response.ok)
                throw new Error(
                  `the EPUB could not be fetched (${response.status})`,
                );
              return new File([await response.blob()], "book.epub", {
                type: "application/epub+zip",
              });
            },
          ),
        ]);
        if (dead) return;
        const m = {
          ...loadedManifest,
          atoms: loadedManifest.atoms.map((atom) =>
            typeof atom.href === "string" ? atom : { ...atom, href: "" },
          ),
        };
        const loadedPreferences = normalizeReaderPreferences(remotePreferences);
        let loadedMarks: ReaderMark[];
        if (remoteMarks.status === "fulfilled") {
          loadedMarks = remoteMarks.value.marks;
          hostedMarksRef.current = true;
        } else {
          const error = remoteMarks.reason;
          if (error instanceof ApiError && error.status === 404) {
            hostedMarksRef.current = false;
            localMarksRef.current = readLocalReaderMarks(bookId);
            loadedMarks = visibleReaderMarks(
              localMarksRef.current,
              saved.bookmark,
            );
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

        const book = await makeBook(file);
        ownedBook = book as { destroy?: () => void };
        if (dead) return;
        const view = document.createElement("foliate-view") as FoliateView;
        ownedView = view;
        holder.current?.replaceChildren(view);

        // one spine-wide mapping — per-relocate href guessing is how front matter could
        // suffix-steal a late atom (see offset.ts)
        const openedBook = book as {
          sections: { id: string }[];
          toc?: EpubTocItem[] | null;
        };
        const bookSections = openedBook.sections;
        const sectionAtom = buildSectionAtomMap(
          m.atoms,
          bookSections.map((s) => s.id),
        );
        sectionAtomRef.current = sectionAtom;
        setToc(
          mapReaderToc(
            openedBook.toc ?? [],
            bookSections.map((s) => s.id),
            sectionAtom,
            m.atoms,
          ),
        );
        const sectionIndexOf = (href: string) => {
          const clean = normalizeHref(href);
          return bookSections.findIndex((s) => normalizeHref(s.id) === clean);
        };
        const annotationFor = (mark: HighlightMark) => ({
          value: mark.anchor.cfi,
          color: HIGHLIGHT_COLORS[mark.color],
        });

        view.addEventListener("draw-annotation", (event) => {
          const detail = (
            event as CustomEvent<{
              draw: (renderer: unknown, options: { color: string }) => void;
              annotation: { color: string };
            }>
          ).detail;
          detail.draw(Overlayer.highlight, { color: detail.annotation.color });
        });
        view.addEventListener("create-overlay", () => {
          for (const mark of marksRef.current) {
            if (mark.kind === "highlight")
              void view.addAnnotation(annotationFor(mark));
          }
        });
        view.addEventListener("load", (event) => {
          const { doc, index } = (
            event as CustomEvent<{ doc: Document; index: number }>
          ).detail;
          const captureSelection = () => {
            if (dead) return;
            const selected = doc.getSelection();
            if (!selected || selected.rangeCount !== 1 || selected.isCollapsed)
              return;
            const atom = sectionAtom[index] ?? -1;
            if (atom < 0) return;
            const text = selected
              .toString()
              .replace(/\s+/g, " ")
              .trim()
              .slice(0, 2000);
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
          };
          doc.addEventListener("pointerup", captureSelection);
          doc.addEventListener("keyup", (event) => {
            if (event.key === "Shift" || event.shiftKey) captureSelection();
          });
        });

        view.addEventListener("relocate", (e) => {
          if (dead) return;
          setFinishCandidate(null);
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
          const verdict = admitRelocation(
            atom,
            lastAtomRef.current,
            maxAtomRef.current,
            allowJumpRef.current,
          );
          if (!verdict.admit) return; // a far landing without explicit confirmation never reports
          if (verdict.spendToken) allowJumpRef.current = false;
          lastAtomRef.current = atom;
          maxAtomRef.current = Math.max(maxAtomRef.current, atom);
          setCurrentAtom(atom);
          setCurrentAnchor({ cfi: d.cfi, atom: atom + 1 });
          const r = view.renderer;
          const page = r.page ?? 1;
          const pages = r.pages ?? 1;
          const finalOffset = finalPageOffset(
            m.atoms,
            m.mode,
            atom,
            page,
            pages,
          );
          if (finalOffset !== null && !positionReportingPausedRef.current)
            setFinishCandidate({ cfi: d.cfi, offset: finalOffset });
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
        if (dead) return;
        disposeMotion = bindReaderMotion(view.renderer);
        for (const mark of marksRef.current) {
          if (mark.kind === "highlight")
            await view.addAnnotation(annotationFor(mark));
          if (dead) return;
        }
        const updateHistory = () =>
          setHistoryState({
            back: view.history.canGoBack,
            forward: view.history.canGoForward,
          });
        view.history.addEventListener("index-change", updateHistory);
        disposeHistory = () =>
          view.history.removeEventListener("index-change", updateHistory);
        // Set both iframe typography and paginator geometry before the first section is initialized,
        // so remote preferences never cause a visible post-render reflow on resume.
        applyReaderPreferences(view.renderer, loadedPreferences);
        // Older imports may have a completed-chapter bookmark without a CFI. Resume at the next
        // unread atom only when the whole-spine map validates its file; never guess a spine index.
        if (saved.cfi) await view.init({ lastLocation: saved.cfi });
        else {
          const atomIndex =
            Number.isSafeInteger(saved.bookmark) && saved.bookmark > 0
              ? Math.min(saved.bookmark, m.atoms.length - 1)
              : -1;
          const href = m.atoms[atomIndex]?.href;
          const externalHref = /^(?:[a-z][a-z\d+.-]*:|\/\/)/i;
          const section =
            href && !externalHref.test(href)
              ? sectionAtom.findIndex(
                  (mappedAtom) =>
                    mappedAtom >= 0 &&
                    (mappedAtom === atomIndex ||
                      (m.mode === "anchor-driven" &&
                        normalizeHref(m.atoms[mappedAtom].href) ===
                          normalizeHref(href))),
                )
              : -1;
          const sectionHref = bookSections[section]?.id;
          const fragment = href?.includes("#")
            ? href.slice(href.indexOf("#"))
            : "";
          const target =
            sectionHref && !externalHref.test(sectionHref)
              ? `${sectionHref.split("#")[0]}${fragment}`
              : 0;
          await view.goTo(target);
        }
        if (dead) return;
        viewRef.current = view;
        setReaderReady(true);
        updateHistory();
      } catch (e) {
        download.abort();
        releaseBook();
        if (!dead) setFatal(e instanceof Error ? e.message : String(e));
      } finally {
        initializing = false;
        if (dead) releaseBook();
      }
    })();
    return () => {
      dead = true;
      download.abort();
      window.removeEventListener("pagehide", flush);
      window.removeEventListener("online", retry);
      window.removeEventListener("litlet:offline-sync", reconcileOffline);
      // Persist before disposal, including a Finish book affirmation waiting behind a live PUT.
      // Replay follows that PUT's acknowledgment so the old request cannot overwrite a newer CFI.
      void reporter.drainAndDispose(retainPosition).then(() => {
        if (activeOfflineOwner() === owner) return flushOfflineMutations();
      }).catch(() => {});
      if (reporterRef.current === reporter) reporterRef.current = null;
      preferenceSaver.drainAndDispose();
      if (preferenceSaverRef.current === preferenceSaver) preferenceSaverRef.current = null;
      const closingView = ownedView;
      if (!initializing) releaseBook();
      if (viewRef.current === closingView) {
        viewRef.current = null; // the keyboard/page-nav handlers must never drive a dead book's view
        visibleRangeRef.current = null;
      }
    };
  }, [bookId, publishMarks, report, loadAttempt]);

  // while a full-screen leaf (hero or graph) is open, mark everything behind the scrim inert — so
  // aria-modal is honest: the background is neither tabbable nor exposed to AT (belt to the focus trap)
  const modalOpen =
    heroOpen || codexOpen || askOpen || closeoutOpen || selectionAssist != null;
  // inert everything behind an open full-screen leaf (hero/graph). The close-time un-inert timing is
  // handled synchronously in each leaf's onClose (below) — a reactive effect re-runs too late for the
  // leaf's focus-restore (a11y pass-2, WCAG 2.4.3).
  useEffect(() => {
    const grid = gridRef.current;
    if (!grid) return;
    for (const child of Array.from(grid.children)) {
      if (!child.classList.contains("hero-scrim"))
        (child as HTMLElement).inert = modalOpen;
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
      positionVersionRef.current =
        reset.position_version ?? positionVersionRef.current + 1;
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
          : Promise.resolve(
              publishMarks(visibleReaderMarks(localMarksRef.current, 0)),
            ),
        viewRef.current?.goTo(0) ?? Promise.resolve(),
      ]);
      for (const result of followups) {
        if (result.status === "rejected")
          console.error("new-pass view refresh failed", result.reason);
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

  const searchGeneration = useRef(0);
  const searchReadPages = useCallback(
    async (query: string): Promise<ReaderSearchResult[]> => {
      const view = viewRef.current;
      if (!view) return [];
      const generation = ++searchGeneration.current;
      const epoch = positionEpochRef.current;
      const current = () =>
        generation === searchGeneration.current &&
        viewRef.current === view &&
        positionEpochRef.current === epoch;
      const completed = pos?.bookmark ?? 0;
      const results: ReaderSearchResult[] = [];
      const seen = new Set<string>();
      const include = async (candidate: {
        cfi: string;
        excerpt: ReaderSearchExcerpt;
      }) => {
        const tocItem = await view.getTOCItemOf(candidate.cfi);
        if (!current()) return;
        const section = view.resolveNavigation(candidate.cfi)?.index ?? -1;
        const tocAtom = tocAtomForHref(toc, tocItem?.href);
        const atom =
          tocAtom ??
          (manifest?.mode === "anchor-driven"
            ? -1
            : (sectionAtomRef.current[section] ?? -1));
        if (
          atom >= 0 &&
          atom < completed &&
          results.length < 100 &&
          !seen.has(candidate.cfi)
        ) {
          seen.add(candidate.cfi);
          results.push({ ...candidate, atom });
        }
      };
      try {
        // Do not download, parse, and scan the unread spine just to discard its results. Shared-file
        // (anchor-driven) chapters still use the per-result frontier check below.
        for (let index = 0; index < sectionAtomRef.current.length; index += 1) {
          const atom = sectionAtomRef.current[index];
          if (atom < 0 || atom >= completed) continue;
          if (!current() || results.length >= 100) break;
          for await (const result of view.search({
            query,
            index,
            // The results list owns highlighting. Suppress raw engine annotations, including matches
            // in an unread fragment of an otherwise eligible shared-file chapter.
            draw: () =>
              document.createElementNS("http://www.w3.org/2000/svg", "g"),
          })) {
            if (!current()) break;
            if (typeof result === "string") continue;
            if (result.cfi && result.excerpt)
              await include(
                result as {
                  cfi: string;
                  excerpt: ReaderSearchExcerpt;
                },
              );
            for (const candidate of result.subitems ?? [])
              await include(candidate);
            if (results.length >= 100) break;
          }
        }
      } finally {
        // A superseded search must not clear the current request's engine bookkeeping.
        if (generation === searchGeneration.current) view.clearSearch();
      }
      return current() ? results : [];
    },
    [manifest?.mode, pos?.bookmark, toc],
  );

  const appendReaderMark = useCallback(
    (mark: ReaderMark) => {
      if (!hostedMarksRef.current) {
        localMarksRef.current = [...localMarksRef.current, mark];
        writeLocalReaderMarks(bookId, localMarksRef.current);
      }
      publishMarks([...marksRef.current, mark]);
    },
    [bookId, publishMarks],
  );

  const clearReaderSelection = useCallback(() => {
    viewRef.current?.deselect();
    setReaderSelection(null);
  }, []);

  const saveHighlight = useCallback(
    async (color: HighlightMark["color"]) => {
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
        setMarksStatus(
          mark.pending ? "highlight queued for sync" : "highlight saved",
        );
      } catch {
        setMarksStatus("highlight could not be saved");
      }
    },
    [appendReaderMark, bookId, clearReaderSelection, readerSelection],
  );

  const saveAnnotation = useCallback(
    async (body: string) => {
      if (!readerSelection) return;
      setMarksStatus("saving note…");
      try {
        const mark = hostedMarksRef.current
          ? await api.createAnnotation(bookId, {
              anchor: readerSelection.anchor,
              body,
            })
          : localAnnotation(readerSelection.anchor, body);
        appendReaderMark(mark);
        clearReaderSelection();
        setMarksStatus(mark.pending ? "note queued for sync" : "note saved");
      } catch {
        setMarksStatus("note could not be saved");
      }
    },
    [appendReaderMark, bookId, clearReaderSelection, readerSelection],
  );

  const saveBookmark = useCallback(
    async (label: string) => {
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
        setMarksStatus(
          mark.pending ? "bookmark queued for sync" : "bookmark saved",
        );
      } catch {
        setMarksStatus("bookmark could not be saved");
      }
    },
    [appendReaderMark, bookId, currentAnchor],
  );

  const deleteReaderMark = useCallback(
    async (mark: ReaderMark) => {
      setMarksStatus(`deleting ${mark.kind}…`);
      try {
        if (hostedMarksRef.current) await api.deleteReaderMark(bookId, mark);
        else {
          localMarksRef.current = localMarksRef.current.filter(
            (item) => item.id !== mark.id,
          );
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
    },
    [bookId, publishMarks],
  );

  const chapterLabel =
    pos && manifest
      ? manifest.atoms[Math.min(pos.current_chapter, manifest.atoms.length) - 1]
          ?.title || ""
      : "";
  const unit = presentationFor(
    manifest?.book_profile?.book_type ?? "novel",
  ).unit;

  return (
    <div
      className="reader-grid"
      ref={gridRef}
      data-reader-theme={preferences.theme}
      data-focus-mode={(focusMode && !compactShell) || undefined}
    >
      <a className="skip-link" href="#main">
        skip to the book
      </a>
      <main id="main" className="book-pane" tabIndex={-1}>
        <header className="running-head">
          <div className="reader-titlebar">
            <button
              className="plain reader-library"
              aria-label="the shelf"
              title="Back to your library"
              onClick={onBack}
            >
              <Icon name="arrowLeft" />
            </button>
            <div className="reader-title-group">
              <h1 className="title">{title || "Opening your book…"}</h1>
              <span className="chapter">
                {chapterLabel || "Your reading space"}
              </span>
            </div>
            <span
              className={`sync-state ${syncState}`}
              role="status"
              aria-live="polite"
            >
              {syncState === "saving" || syncState === "pending"
                ? "Saving…"
                : syncState === "queued"
                  ? "Waiting to sync"
                  : syncState === "conflict"
                    ? "Newer progress kept"
                    : syncState === "error"
                      ? "Sync needs attention"
                      : "Saved"}
            </span>
            <button
              className="plain reader-companion-toggle"
              aria-label="Focus mode"
              aria-pressed={focusMode}
              title={
                focusMode
                  ? "Show the companion"
                  : "Hide the companion for focused reading"
              }
              onClick={() => setFocusMode((value) => !value)}
            >
              <Icon name="book" size={16} />
              <span>Focus</span>
            </button>
          </div>
          <div
            className="reader-toolbar"
            role="group"
            aria-label="Reading tools"
          >
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
              onSearchNavigate={(result) => {
                void viewRef.current?.select(result.cfi);
              }}
            />
            <ReaderMarks
              marks={marks}
              selection={readerSelection}
              currentAnchor={currentAnchor}
              exportUrl={
                hostedMarksRef.current
                  ? api.readerMarksExportUrl(bookId)
                  : `data:application/json;charset=utf-8,${encodeURIComponent(
                      JSON.stringify({
                        format: "litlet-reader-marks",
                        version: 1,
                        book_id: bookId,
                        as_of_chapter: pos?.bookmark ?? 0,
                        marks,
                      }),
                    )}`
              }
              status={marksStatus}
              onAssist={(action) => {
                if (readerSelection)
                  setSelectionAssist({ action, selection: readerSelection });
              }}
              onHighlight={(color) => {
                void saveHighlight(color);
              }}
              onAnnotate={(body) => {
                void saveAnnotation(body);
              }}
              onBookmark={(label) => {
                void saveBookmark(label);
              }}
              onNavigate={(mark) => {
                void viewRef.current?.goTo(mark.anchor.cfi);
              }}
              onDelete={(mark) => {
                void deleteReaderMark(mark);
              }}
              onClearSelection={clearReaderSelection}
            />
            <span className="reader-toolbar-spacer" />
            {pos && pos.bookmark > 0 && (
              <button
                className="plain reader-reset"
                aria-label="Start over"
                onClick={() => {
                  positionReportingPausedRef.current = true;
                  setStartOverError(null);
                  setStartOverOpen(true);
                }}
              >
                <Icon name="refresh" size={14} />
                <span>Start over</span>
              </button>
            )}
          </div>
        </header>
        <div
          className="book-view"
          ref={holder}
          role="region"
          aria-label="Book pages"
        />
        {!readerReady && !fatal && (
          <div className="reader-loading" role="status">
            <Icon name="book" size={30} />
            <span>Opening your book…</span>
          </div>
        )}
        {fatal && (
          <div className="reader-fatal" role="alert">
            <Icon name="book" size={30} />
            <h2>A moment between chapters.</h2>
            <p>The book could not be opened: {fatal}</p>
            <div className="reader-fatal-actions">
              <Button
                variant="primary"
                icon="refresh"
                onClick={() => setLoadAttempt((attempt) => attempt + 1)}
              >
                Try again
              </Button>
              <Button onClick={onBack}>Back to library</Button>
            </div>
          </div>
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
        <footer className="reader-footer">
          <span>
            {pos && manifest
              ? `${unit[0].toUpperCase() + unit.slice(1)} ${Math.min(pos.current_chapter, manifest.atoms.length)} of ${manifest.atoms.length}`
              : "Preparing your pages"}
          </span>
          <span className="reader-page-hint">Use ← → to turn the page</span>
          <div className="reader-page-buttons">
            {finishCandidate &&
              manifest &&
              readerReady &&
              !startOverOpen &&
              !pendingJump &&
              (pos?.bookmark ?? 0) < manifest.atoms.length && (
                <Button
                  variant="primary"
                  disabled={syncState === "saving"}
                  onClick={() => {
                    if (positionReportingPausedRef.current) return;
                    reporterRef.current?.complete(
                      finishCandidate.cfi,
                      finishCandidate.offset,
                      manifest.atoms.length,
                      positionEpochRef.current,
                    );
                  }}
                >
                  Finish book
                </Button>
              )}
            <button
              className="page-nav prev"
              aria-label="Previous page"
              disabled={!readerReady || startOverOpen || !!pendingJump}
              onClick={() => viewRef.current?.prev()}
            >
              <Icon name="arrowLeft" size={17} />
            </button>
            <button
              className="page-nav next"
              aria-label="Next page"
              disabled={!readerReady || startOverOpen || !!pendingJump}
              onClick={() => viewRef.current?.next()}
            >
              <Icon name="arrowRight" size={17} />
            </button>
          </div>
        </footer>
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
        desktopHidden={focusMode}
      />
      {heroOpen && (
        <CatchMeUp
          bookId={bookId}
          bookmark={pos?.bookmark ?? 0}
          totalAtoms={manifest?.atoms.length ?? 0}
          bookType={manifest?.book_profile?.book_type ?? "novel"}
          welcomeBackChapter={welcomeBack?.lastChapter ?? null}
          onClose={() => {
            unInertGrid(); // synchronous un-inert so the hero's focus-restore reaches the opener
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
