import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Book } from "../api";
import { InstallApp } from "../pwa/InstallApp";
import {
  activeOfflineOwner,
  cacheBookForOffline,
  isBookSavedOffline,
  type OfflineSession,
} from "../pwa/offline";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { ProviderSettings } from "./ProviderSettings";

const recentKey = (owner: string) => `litlet.library-recent:${owner}`;
function readRecent(owner: string): string[] {
  try {
    const value: unknown = JSON.parse(
      localStorage.getItem(recentKey(owner)) ?? "[]",
    );
    return Array.isArray(value)
      ? value.filter((id): id is string => typeof id === "string").slice(0, 20)
      : [];
  } catch {
    return [];
  }
}
const coverTones = ["olive", "clay", "blue", "plum", "sand", "forest"];
export function BookCover({
  book,
  className = "",
}: {
  book: Book;
  className?: string;
}) {
  const tone =
    coverTones[
      Array.from(book.title).reduce(
        (sum, char) => sum + char.charCodeAt(0),
        0,
      ) % coverTones.length
    ];
  return (
    <span
      className={`book-cover cover-${tone} ${className}`}
      aria-hidden="true"
    >
      <span className="cover-edition">READING COMPANION</span>
      <span className="cover-title">{book.title}</span>
      <span className="cover-ornament">
        <span />
        <Icon name="book" size={24} />
        <span />
      </span>
      <span className="cover-author">{book.author || "Your collection"}</span>
    </span>
  );
}

export function Shelf({
  onOpen,
  session,
}: {
  onOpen: (id: string) => void;
  session?: OfflineSession | null;
}) {
  const owner = activeOfflineOwner();
  const [books, setBooks] = useState<Book[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [settingsAvailable, setSettingsAvailable] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [offlineBusy, setOfflineBusy] = useState<string | null>(null);
  const [offlineState, setOfflineState] = useState(() => ({
    owner,
    ids: new Set<string>(),
  }));
  const offlineIds =
    offlineState.owner === owner ? offlineState.ids : new Set<string>();
  const [offlineStatus, setOfflineStatus] = useState("");
  const [signingOut, setSigningOut] = useState(false);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<"all" | "recent" | "offline">("all");
  const [sort, setSort] = useState("added");
  const [layout, setLayout] = useState<"grid" | "list">("grid");
  const [dragging, setDragging] = useState(false);
  const [recentState, setRecent] = useState(() => ({
    owner,
    ids: readRecent(owner),
  }));
  const recent =
    recentState.owner === owner ? recentState.ids : readRecent(owner);
  const fileInput = useRef<HTMLInputElement>(null);
  const dragDepth = useRef(0);
  const importLock = useRef(false);
  const mounted = useRef(true);
  const msg = (error: unknown) =>
    error instanceof Error ? error.message : String(error);
  const refresh = useCallback(async () => {
    setErr(null);
    try {
      const result = await api.books();
      if (mounted.current) setBooks(result);
    } catch (error) {
      if (mounted.current) setErr(msg(error));
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    void refresh();
    api
      .providerSettings()
      .then(() => {
        if (mounted.current) setSettingsAvailable(true);
      })
      .catch(() => {});
    return () => {
      mounted.current = false;
    };
  }, [refresh]);
  useEffect(() => {
    let dead = false;
    if (!books || !("caches" in globalThis)) return;
    void Promise.all(
      books.map(async (book) =>
        (await isBookSavedOffline(book.book_id, owner)) ? book.book_id : null,
      ),
    ).then((saved) => {
      if (!dead)
        setOfflineState({
          owner,
          ids: new Set(saved.filter((id): id is string => id !== null)),
        });
    });
    return () => {
      dead = true;
    };
  }, [books, owner]);
  const onFile = async (file: File | undefined) => {
    if (!file || importLock.current) return;
    if (!/\.epub$/i.test(file.name) && file.type !== "application/epub+zip") {
      setErr("Choose a DRM-free EPUB file to add to your library.");
      return;
    }
    importLock.current = true;
    setBusy(true);
    setErr(null);
    try {
      await api.importBook(file);
      await refresh();
    } catch (error) {
      if (mounted.current) setErr(msg(error));
    } finally {
      importLock.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  const saveOffline = async (book: Book) => {
    if (offlineBusy) return;
    setOfflineBusy(book.book_id);
    setOfflineStatus(`Saving ${book.title} for offline reading…`);
    try {
      await cacheBookForOffline(book.book_id);
      setOfflineState((current) => ({
        owner,
        ids: new Set([
          ...(current.owner === owner ? current.ids : []),
          book.book_id,
        ]),
      }));
      setOfflineStatus(`${book.title} is ready for offline reading.`);
    } catch (error) {
      setOfflineStatus(msg(error));
    } finally {
      setOfflineBusy(null);
    }
  };
  const openBook = (id: string) => {
    const next = [id, ...recent.filter((value) => value !== id)].slice(0, 20);
    setRecent({ owner, ids: next });
    try {
      localStorage.setItem(recentKey(owner), JSON.stringify(next));
    } catch {
      /* Optional convenience state. */
    }
    onOpen(id);
  };
  const logout = async () => {
    setSigningOut(true);
    setErr(null);
    try {
      await api.logout();
      location.assign("/api/auth/login?return_to=/");
    } catch (error) {
      setErr(msg(error));
      setSigningOut(false);
    }
  };
  const featured = books?.find((book) => book.book_id === recent[0]);
  const filtered = (books ?? [])
    .filter(
      (book) =>
        `${book.title} ${book.author ?? ""}`
          .toLocaleLowerCase()
          .includes(query.trim().toLocaleLowerCase()) &&
        (filter !== "offline" || offlineIds.has(book.book_id)) &&
        (filter !== "recent" || recent.includes(book.book_id)),
    )
    .sort((a, b) =>
      sort === "title"
        ? a.title.localeCompare(b.title)
        : sort === "author"
          ? (a.author ?? "").localeCompare(b.author ?? "")
          : filter === "recent"
            ? recent.indexOf(a.book_id) - recent.indexOf(b.book_id)
            : 0,
    );

  return (
    <div
      className={`library-shell ${dragging ? "is-dragging" : ""}`}
      onDragEnter={(event) => {
        if (!event.dataTransfer.types.includes("Files")) return;
        event.preventDefault();
        dragDepth.current++;
        setDragging(true);
      }}
      onDragOver={(event) => {
        if (event.dataTransfer.types.includes("Files")) event.preventDefault();
      }}
      onDragLeave={(event) => {
        event.preventDefault();
        if (--dragDepth.current <= 0) {
          dragDepth.current = 0;
          setDragging(false);
        }
      }}
      onDrop={(event) => {
        event.preventDefault();
        dragDepth.current = 0;
        setDragging(false);
        void onFile(event.dataTransfer.files[0]);
      }}
    >
      <a className="skip-link" href="#library-main">
        Skip to your library
      </a>
      <aside className="library-sidebar" aria-label="Library navigation">
        <a className="brand" href="/" aria-label="Reading Companion home">
          <span className="brand-mark">
            <Icon name="book" size={23} />
          </span>
          <span>
            reading
            <br />
            <strong>companion</strong>
          </span>
        </a>
        <div className="sidebar-section-label">YOUR SPACE</div>
        <nav className="library-nav" aria-label="Your books">
          <button
            aria-label="All books"
            aria-current={filter === "all" ? "page" : undefined}
            onClick={() => setFilter("all")}
          >
            <Icon name="library" />
            <span className="nav-label-full">All books</span>
            <span className="nav-label-short" aria-hidden="true">
              All books
            </span>
            {books && <span className="nav-count">{books.length}</span>}
          </button>
          <button
            aria-label="Recently opened"
            aria-current={filter === "recent" ? "page" : undefined}
            onClick={() => setFilter("recent")}
          >
            <Icon name="book" />
            <span className="nav-label-full">Recently opened</span>
            <span className="nav-label-short" aria-hidden="true">
              Recent
            </span>
          </button>
          <button
            aria-label="Available offline"
            aria-current={filter === "offline" ? "page" : undefined}
            onClick={() => setFilter("offline")}
          >
            <Icon name="download" />
            <span className="nav-label-full">Available offline</span>
            <span className="nav-label-short" aria-hidden="true">
              Offline
            </span>
          </button>
        </nav>
        <Button
          variant="ghost"
          className="sidebar-import"
          icon="plus"
          onClick={() => fileInput.current?.click()}
          disabled={busy}
        >
          Add a book
        </Button>
        <div className="sidebar-bottom">
          <div className="sidebar-note">
            <Icon name="shield" size={20} />
            <strong>Your story. At your pace.</strong>
            <p>Your companion only remembers the chapters you have finished.</p>
          </div>
          {settingsAvailable && (
            <Button
              variant="ghost"
              icon="settings"
              onClick={() => setShowSettings(true)}
            >
              AI provider settings
            </Button>
          )}
          <InstallApp />
          {session?.user && (
            <span className="account-name">{session.user.display_name}</span>
          )}
          {session === null && (
            <a className="account-link" href="/api/auth/login?return_to=/">
              Sign in
            </a>
          )}
          {session?.user && (
            <Button
              variant="ghost"
              className="account-signout"
              icon="arrowRight"
              aria-label={
                signingOut ? "Signing out" : "Sign out and erase offline books"
              }
              title="Sign out and erase offline books"
              disabled={signingOut}
              onClick={() => {
                void logout();
              }}
            >
              {signingOut ? "Signing out…" : "Sign out and erase offline books"}
            </Button>
          )}
        </div>
      </aside>
      <main id="library-main" className="library-main" tabIndex={-1}>
        <header className="library-topbar">
          <span>My library</span>
          <span className="safe-label">
            <Icon name="shield" size={15} />
            Spoiler safe, always
          </span>
        </header>
        <div className="library-content">
          <section className="library-intro">
            <div>
              <p className="eyebrow">A GOOD PLACE TO GET LOST</p>
              <h1>
                {featured
                  ? "Back to your story."
                  : "Your next chapter starts here."}
              </h1>
              <p>Stay in the story. We’ll remember the details.</p>
            </div>
            <Button
              variant="primary"
              icon="plus"
              aria-label={busy ? "adding a book" : "add an epub"}
              aria-busy={busy}
              disabled={busy}
              onClick={() => fileInput.current?.click()}
            >
              {busy ? "Adding your book…" : "Add a book"}
            </Button>
          </section>
          {err && (
            <div className="notice notice--error" role="alert">
              <Icon name="info" />
              <span>{err}</span>
              {books === null && (
                <Button
                  variant="ghost"
                  icon="refresh"
                  onClick={() => {
                    void refresh();
                  }}
                >
                  Try again
                </Button>
              )}
            </div>
          )}
          {featured && filter === "all" && !query && (
            <section
              className="continue-card"
              aria-label="Recently opened book"
            >
              <BookCover book={featured} className="continue-cover" />
              <div className="continue-copy">
                <span className="eyebrow">PICK UP WHERE YOU LEFT OFF</span>
                <h2>{featured.title}</h2>
                <p className="continue-author">{featured.author}</p>
                <p className="continue-description">
                  The next page is waiting. Your place and story memory are
                  right where you left them.
                </p>
                <Button
                  variant="primary"
                  icon="arrowRight"
                  onClick={() => openBook(featured.book_id)}
                >
                  Continue reading
                </Button>
              </div>
              <div className="continue-art" aria-hidden="true">
                <span />
                <span />
                <Icon name="book" size={48} />
              </div>
            </section>
          )}
          <section
            className="library-collection"
            aria-labelledby="collection-title"
          >
            <div className="collection-heading">
              <h2 id="collection-title">
                {filter === "recent"
                  ? "Recently opened"
                  : filter === "offline"
                    ? "Available offline"
                    : "Your books"}
                <span>{books ? filtered.length : ""}</span>
              </h2>
              <div className="collection-tools">
                <label className="library-search">
                  <Icon name="search" size={17} />
                  <span className="sr-only">Search your library</span>
                  <input
                    type="search"
                    placeholder="Search books or authors"
                    value={query}
                    onChange={(event) => setQuery(event.currentTarget.value)}
                  />
                </label>
                <label className="library-sort">
                  <span className="sr-only">Sort books</span>
                  <select
                    value={sort}
                    onChange={(event) => setSort(event.currentTarget.value)}
                  >
                    <option value="added">Library order</option>
                    <option value="title">Title A–Z</option>
                    <option value="author">Author A–Z</option>
                  </select>
                </label>
                <div
                  className="view-switch"
                  role="group"
                  aria-label="Library layout"
                >
                  <button
                    aria-label="Grid view"
                    aria-pressed={layout === "grid"}
                    onClick={() => setLayout("grid")}
                  >
                    <Icon name="grid" size={17} />
                  </button>
                  <button
                    aria-label="List view"
                    aria-pressed={layout === "list"}
                    onClick={() => setLayout("list")}
                  >
                    <Icon name="list" size={18} />
                  </button>
                </div>
              </div>
            </div>
            {books === null && !err ? (
              <div
                className="library-loading"
                aria-label="Loading your library"
                aria-busy="true"
              >
                {[0, 1, 2, 3].map((item) => (
                  <div className="book-skeleton" key={item}>
                    <span />
                    <span />
                    <span />
                  </div>
                ))}
              </div>
            ) : filtered.length ? (
              <div
                className={`book-grid ${layout === "list" ? "book-grid--list" : ""}`}
              >
                {filtered.map((book) => (
                  <article className="book-card" key={book.book_id}>
                    <button
                      className="book-open"
                      aria-label={
                        book.author
                          ? `${book.title}, ${book.author}`
                          : book.title
                      }
                      onClick={() => openBook(book.book_id)}
                    >
                      <BookCover book={book} />
                      <span className="book-metadata">
                        <span
                          className="book-title"
                          id={`book-${book.book_id}-title`}
                        >
                          {book.title}
                        </span>
                        <span className="book-author">
                          {book.author || "Unknown author"}
                        </span>
                        <span className="book-format">
                          EPUB <span>·</span>{" "}
                          {recent.includes(book.book_id)
                            ? "Recently opened"
                            : "Ready to read"}
                        </span>
                      </span>
                      <span className="book-open-arrow">
                        <Icon name="arrowRight" />
                      </span>
                    </button>
                    <button
                      className={`offline-book ${offlineIds.has(book.book_id) ? "is-saved" : ""}`}
                      disabled={!!offlineBusy}
                      aria-label={
                        offlineIds.has(book.book_id)
                          ? "refresh offline copy"
                          : "save for offline reading"
                      }
                      aria-describedby={`book-${book.book_id}-title`}
                      onClick={() => {
                        void saveOffline(book);
                      }}
                    >
                      <Icon
                        name={
                          offlineIds.has(book.book_id) ? "check" : "download"
                        }
                        size={15}
                      />
                      {offlineBusy === book.book_id
                        ? "Saving…"
                        : offlineIds.has(book.book_id)
                          ? "Saved offline"
                          : "Save offline"}
                    </button>
                  </article>
                ))}
              </div>
            ) : (
              books !== null && (
                <div className="library-empty">
                  <span className="empty-icon">
                    <Icon
                      name={
                        query
                          ? "search"
                          : filter === "offline"
                            ? "download"
                            : "book"
                      }
                      size={28}
                    />
                  </span>
                  <h3>
                    {query
                      ? "No books found"
                      : filter === "offline"
                        ? "Take your stories with you."
                        : filter === "recent"
                          ? "Your next read is waiting."
                          : "Make yourself at home."}
                  </h3>
                  <p>
                    {query
                      ? "Try a different title or author."
                      : filter === "offline"
                        ? "Choose Save offline on a book to read wherever you go."
                        : filter === "recent"
                          ? "Open a book and it will be easy to find here."
                          : "Add a DRM-free EPUB. Your companion will follow along, one chapter at a time."}
                  </p>
                  {query || filter !== "all" ? (
                    <Button
                      onClick={() => {
                        setQuery("");
                        setFilter("all");
                      }}
                    >
                      Show all books
                    </Button>
                  ) : (
                    <Button
                      variant="primary"
                      icon="plus"
                      disabled={busy}
                      onClick={() => fileInput.current?.click()}
                    >
                      Choose an EPUB
                    </Button>
                  )}
                </div>
              )
            )}
          </section>
          <footer className="library-footer">
            <span>
              <Icon name="book" size={16} />
              Read a little. Remember a lot.
            </span>
            <span>DRM-free EPUBs · Your own collection</span>
          </footer>
        </div>
      </main>
      {showSettings && (
        <ProviderSettings onClose={() => setShowSettings(false)} />
      )}
      {offlineStatus && (
        <div className="library-toast" aria-live="polite">
          <Icon name="download" size={17} />
          {offlineStatus}
          <button
            className="plain"
            aria-label="Dismiss offline message"
            onClick={() => setOfflineStatus("")}
          >
            <Icon name="close" size={17} />
          </button>
        </div>
      )}
      <div role="status" className="sr-only">
        {busy ? "adding a book…" : ""}
      </div>
      {dragging && (
        <div className="import-drop-overlay" aria-hidden="true">
          <Icon name="plus" size={38} />
          <h2>Drop your next read here.</h2>
          <p>DRM-free EPUB files</p>
        </div>
      )}
      <input
        ref={fileInput}
        type="file"
        accept=".epub,application/epub+zip"
        aria-label="import an EPUB file"
        hidden
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          event.currentTarget.value = "";
          void onFile(file);
        }}
      />
    </div>
  );
}
