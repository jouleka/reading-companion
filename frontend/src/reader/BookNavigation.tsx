import { type FormEvent, useEffect, useRef, useState } from "react";
import type { Atom } from "../api";
import { Icon } from "../ui/Icon";
import { Button } from "../ui/Button";
import { useReaderPopover } from "../ui/useReaderPopover";
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
  const { open, closing, id, triggerRef, panelRef, toggle, close } =
    useReaderPopover();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ReaderSearchResult[]>([]);
  const [searchState, setSearchState] = useState<SearchState>("idle");
  const searchRequest = useRef(0);

  useEffect(
    () => () => {
      searchRequest.current += 1;
    },
    [],
  );

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const value = query.trim();
    if (value.length < 2 || searchableChapters < 1) return;
    const request = ++searchRequest.current;
    setSearchState("searching");
    try {
      const found = await onSearch(value);
      if (request !== searchRequest.current) return;
      setResults(found);
      setSearchState("done");
    } catch {
      if (request !== searchRequest.current) return;
      setResults([]);
      setSearchState("error");
    }
  };

  return (
    <div className="reader-navigation">
      <button
        ref={triggerRef}
        type="button"
        className="plain reader-tool navigation-trigger"
        aria-label="contents"
        aria-expanded={open && !closing}
        aria-controls={id}
        onClick={toggle}
      >
        <Icon name="list" />
        <span>Contents</span>
      </button>
      {open && (
        <section
          id={id}
          className="reader-popover navigation-panel"
          data-closing={closing || undefined}
          aria-hidden={closing || undefined}
          role="region"
          aria-label="Book navigation"
          tabIndex={-1}
          ref={panelRef}
        >
          <div className="reader-panel-heading navigation-heading">
            <h2>Book navigation</h2>
            <Button
              variant="ghost"
              className="icon-button"
              aria-label="Close book navigation"
              onClick={() => close()}
            >
              <Icon name="close" />
            </Button>
          </div>
          <div className="history-controls" aria-label="Navigation history">
            <Button
              variant="ghost"
              icon="arrowLeft"
              disabled={!canGoBack}
              onClick={onBack}
            >
              back
            </Button>
            <Button
              variant="ghost"
              disabled={!canGoForward}
              onClick={onForward}
            >
              <span>forward</span> <Icon name="arrowRight" size={16} />
            </Button>
          </div>
          <form
            className="book-search"
            role="search"
            onSubmit={(event) => void submit(event)}
          >
            <label htmlFor={`${id}-query`}>Find in pages you have read</label>
            <div>
              <input
                id={`${id}-query`}
                type="search"
                value={query}
                minLength={2}
                maxLength={200}
                placeholder="Search completed chapters"
                disabled={searchableChapters < 1}
                onChange={(event) => {
                  searchRequest.current += 1;
                  setQuery(event.currentTarget.value);
                  setResults([]);
                  setSearchState("idle");
                }}
              />
              <Button
                type="submit"
                variant="primary"
                icon="search"
                disabled={
                  query.trim().length < 2 ||
                  searchableChapters < 1 ||
                  searchState === "searching"
                }
              >
                find
              </Button>
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
                  <button
                    type="button"
                    className="plain"
                    onClick={() => {
                      onSearchNavigate(result);
                      close();
                    }}
                  >
                    <span>
                      {atoms[result.atom]?.title ||
                        `Chapter ${result.atom + 1}`}
                    </span>
                    <q>
                      {result.excerpt.pre}
                      <mark>{result.excerpt.match}</mark>
                      {result.excerpt.post}
                    </q>
                  </button>
                </li>
              ))}
            </ol>
          )}
          <nav className="reader-toc" aria-label="Table of contents">
            {toc.length > 0 ? (
              <TocBranch
                items={toc}
                atoms={atoms}
                currentAtom={currentAtom}
                onNavigate={(item) => {
                  onNavigate(item);
                  close();
                }}
              />
            ) : (
              <p className="quiet">This book has no table of contents.</p>
            )}
          </nav>
        </section>
      )}
    </div>
  );
}
