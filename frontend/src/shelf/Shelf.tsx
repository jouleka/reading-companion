/** The library table: books as spines standing on a double-ruled shelf; import = the empty slot. */
import { useEffect, useRef, useState } from "react";
import { api, type Book } from "../api";
import { InstallApp } from "../pwa/InstallApp";
import { cacheBookForOffline, type OfflineSession } from "../pwa/offline";
import { ProviderSettings } from "./ProviderSettings";

export function Shelf({
  onOpen,
  session,
}: {
  onOpen: (id: string) => void;
  session?: OfflineSession | null;
}) {
  const [books, setBooks] = useState<Book[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [settingsAvailable, setSettingsAvailable] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [offlineBusy, setOfflineBusy] = useState<string | null>(null);
  const [offlineStatus, setOfflineStatus] = useState("");
  const [signingOut, setSigningOut] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const msg = (e: unknown) => (e instanceof Error ? e.message : String(e));
  const refresh = () => api.books().then(setBooks).catch((e) => setErr(msg(e)));
  useEffect(() => {
    refresh();
    api.providerSettings().then(() => setSettingsAvailable(true)).catch(() => {
      setSettingsAvailable(false);
    });
  }, []);

  const onFile = async (f: File | undefined) => {
    if (!f) return;
    setBusy(true);
    setErr(null);
    try {
      await api.importBook(f);
      await refresh();
    } catch (e) {
      setErr(msg(e));
    } finally {
      setBusy(false);
    }
  };

  const saveOffline = async (book: Book) => {
    setOfflineBusy(book.book_id);
    setOfflineStatus(`saving ${book.title} for offline reading…`);
    try {
      await cacheBookForOffline(book.book_id);
      setOfflineStatus(`${book.title} is ready offline`);
    } catch (error) {
      setOfflineStatus(msg(error));
    } finally {
      setOfflineBusy(null);
    }
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

  return (
    <main className="shelf-wrap">
      <header className="shelf-head">
        <h1>
          A reader <em>with a brain.</em>
        </h1>
        <p className="shelf-sub">
          It remembers what you have read — and nothing you have not.
        </p>
        <div className="shelf-account">
          <InstallApp />
          {session?.user && <span>{session.user.display_name}</span>}
          {session === null && <a href="/api/auth/login?return_to=/">sign in</a>}
          {settingsAvailable && (
            <button type="button" className="plain" onClick={() => setShowSettings(true)}>
              AI provider settings
            </button>
          )}
          {session?.user && (
            <button
              type="button"
              className="plain"
              disabled={signingOut}
              onClick={() => { void logout(); }}
            >
              {signingOut ? "signing out…" : "sign out and erase offline books"}
            </button>
          )}
        </div>
      </header>

      {showSettings && <ProviderSettings onClose={() => setShowSettings(false)} />}

      <div className="spine-row">
        {books?.map((b) => (
          <div className="book-slot" key={b.book_id}>
            <button
              type="button"
              className="spine"
              // a spoken name with a separator ("Title, Author"), not the run-together span text
              aria-label={b.author ? `${b.title}, ${b.author}` : b.title}
              onClick={() => onOpen(b.book_id)}
            >
              <span id={`book-${b.book_id}-title`}>{b.title}</span>
              <span className="author">{b.author ?? ""}</span>
            </button>
            <button
              type="button"
              className="plain offline-book"
              disabled={offlineBusy === b.book_id}
              aria-label="save for offline reading"
              aria-describedby={`book-${b.book_id}-title`}
              onClick={() => { void saveOffline(b); }}
            >
              {offlineBusy === b.book_id ? "saving…" : "offline"}
            </button>
          </div>
        ))}
        <button
          type="button"
          className="spine empty"
          aria-label={busy ? "adding a book" : "add an epub"}
          aria-busy={busy}
          disabled={busy}
          onClick={() => fileInput.current?.click()}
        >
          {busy ? "binding…" : "＋ add an epub"}
        </button>
      </div>

      {err && (
        <p className="quiet" style={{ marginTop: 18 }} role="alert">
          ⚠ {err}
        </p>
      )}
      {/* the disabled import button is silent to AT while busy; announce the state separately */}
      <div role="status" className="sr-only">
        {busy ? "adding a book…" : ""}
      </div>
      <div className="sr-only" aria-live="polite">{offlineStatus}</div>
      <input
        ref={fileInput}
        type="file"
        accept=".epub,application/epub+zip"
        aria-label="import an EPUB file"
        style={{ display: "none" }}
        onChange={(e) => {
          const f = e.target.files?.[0];
          e.target.value = ""; // so re-selecting the SAME file after a failure fires change again
          onFile(f);
        }}
      />
    </main>
  );
}
