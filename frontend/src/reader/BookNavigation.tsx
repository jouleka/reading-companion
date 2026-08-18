import { type FormEvent, useId, useRef, useState } from "react";
import type { Atom } from "../api";
import {
  type ReaderSearchResult,
  type ReaderTocItem,
  safeTocLabel,
  tocContainsAtom,
} from "./readerNavigation";

type SearchState = "idle" | "searching" | "done" | "error";

function TocBranch({
  items,
  atoms,
  currentAtom,
  onNavigate,
}: {
  items: ReaderTocItem[];
  atoms: Atom[];
  currentAtom: number;
  onNavigate: (item: ReaderTocItem) => void;
}) {
  return (
    <ul>
      {items.map((item) => {
        const current = item.atom === currentAtom;
        const containsCurrent = !current && tocContainsAtom(item, currentAtom);
        return (
          <li key={item.id} data-current-branch={containsCurrent || undefined}>
            {item.href ? (
              <button
                type="button"
                className="plain toc-link"
                aria-current={current ? "location" : undefined}
                onClick={() => onNavigate(item)}
              >
                {safeTocLabel(item, atoms)}
              </button>
            ) : (
              <span className="toc-group">{safeTocLabel(item, atoms)}</span>
            )}
            {item.children.length > 0 && (
              <TocBranch
                items={item.children}
                atoms={atoms}
                currentAtom={currentAtom}
                onNavigate={onNavigate}
              />
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function ReaderNavigation({
  toc,
  atoms,
  currentAtom,
  canGoBack,
  canGoForward,
  searchableChapters,
  onBack,
  onForward,
  onNavigate,
  onSearch,
  onSearchNavigate,
}: {
  toc: ReaderTocItem[];
  atoms: Atom[];
  currentAtom: number;
  canGoBack: boolean;
  canGoForward: boolean;
  searchableChapters: number;
  onBack: () => void;
  onForward: () => void;
  onNavigate: (item: ReaderTocItem) => void;
  onSearch: (query: string) => Promise<ReaderSearchResult[]>;
  onSearchNavigate: (result: ReaderSearchResult) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ReaderSearchResult[]>([]);
  const [searchState, setSearchState] = useState<SearchState>("idle");
  const id = useId();
  const opener = useRef<HTMLButtonElement>(null);

  const close = () => {
    setOpen(false);
    opener.current?.focus();
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const value = query.trim();
    if (value.length < 2 || searchableChapters < 1) return;
    setSearchState("searching");
    try {
      const found = await onSearch(value);
      setResults(found);
      setSearchState("done");
    } catch {
      setResults([]);
      setSearchState("error");
    }
  };

  return (
    <div className="reader-navigation">
      <button
        ref={opener}
        type="button"
        className="plain smallcaps navigation-trigger"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((value) => !value)}
      >
        contents
      </button>
      {open && (
        <section
          id={id}
          className="navigation-panel"
          role="region"
          aria-label="Book navigation"
          onKeyDown={(event) => {
            if (event.key === "Escape") close();
          }}
        >
          <div className="navigation-heading">
            <h2>Book navigation</h2>
            <button type="button" className="plain" onClick={close}>close</button>
          </div>
          <div className="history-controls" aria-label="Navigation history">
            <button type="button" className="plain" disabled={!canGoBack} onClick={onBack}>
              ← back
            </button>
            <button type="button" className="plain" disabled={!canGoForward} onClick={onForward}>
              forward →
            </button>
          </div>
          <form className="book-search" role="search" onSubmit={(event) => void submit(event)}>
            <label htmlFor={`${id}-query`}>Find in pages you have read</label>
            <div>
              <input
                id={`${id}-query`}
                type="search"
                value={query}
                minLength={2}
                maxLength={200}
                disabled={searchableChapters < 1}
                onChange={(event) => setQuery(event.currentTarget.value)}
              />
              <button type="submit" disabled={query.trim().length < 2 || searchableChapters < 1}>
                find
              </button>
            </div>
          </form>
          <p className="navigation-status" role="status" aria-live="polite">
            {searchableChapters < 1
              ? "Search becomes available after you finish a chapter."
              : searchState === "searching"
                ? "Searching the pages you have read…"
                : searchState === "error"
                  ? "Search is temporarily unavailable."
                  : searchState === "done"
                    ? `${results.length} result${results.length === 1 ? "" : "s"}.`
                    : ""}
          </p>
          {results.length > 0 && (
            <ol className="book-search-results" aria-label="Search results">
              {results.map((result) => (
                <li key={result.cfi}>
                  <button type="button" className="plain" onClick={() => {
                    onSearchNavigate(result);
                    close();
                  }}>
                    <span>{atoms[result.atom]?.title || `Chapter ${result.atom + 1}`}</span>
                    <q>{result.excerpt.pre}<mark>{result.excerpt.match}</mark>{result.excerpt.post}</q>
                  </button>
                </li>
              ))}
            </ol>
          )}
          <nav className="reader-toc" aria-label="Table of contents">
            {toc.length > 0
              ? <TocBranch
                  items={toc}
                  atoms={atoms}
                  currentAtom={currentAtom}
                  onNavigate={(item) => {
                    onNavigate(item);
                    close();
                  }}
                />
              : <p className="quiet">This book has no table of contents.</p>}
          </nav>
        </section>
      )}
    </div>
  );
}
