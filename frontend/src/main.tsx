import "@fontsource/eb-garamond/400.css";
import "@fontsource/eb-garamond/400-italic.css";
import "@fontsource/fraunces/400.css";
import "./styles.css";
import "./experience.css";

import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { initializeOfflineSupport, type OfflineSession } from "./pwa/offline";
import { ReaderPage } from "./reader/ReaderPage";
import { Shelf } from "./shelf/Shelf";
import { Icon } from "./ui/Icon";
import { Button } from "./ui/Button";
import { AiRuntimeProvider } from "./ui/AiStatus";

// Router-less view switch for the shelf and reader.
const currentBookId = () =>
  new URLSearchParams(window.location.search).get("book");

function App({ session }: { session?: OfflineSession | null }) {
  const [bookId, setBookId] = useState<string | null>(currentBookId);
  useEffect(() => {
    const restoreLocation = () => setBookId(currentBookId());
    window.addEventListener("popstate", restoreLocation);
    return () => window.removeEventListener("popstate", restoreLocation);
  }, []);
  const open = (id: string) => {
    history.pushState(null, "", `/?book=${encodeURIComponent(id)}`);
    setBookId(id);
  };
  const back = () => {
    history.pushState(null, "", "/");
    setBookId(null);
  };
  // key: a book->book transition must fully unmount the old reader (its view/reporter must never
  // route a stale offset into ANOTHER book's permanent bookmark ratchet — pass-2 F4)
  return bookId ? (
    <ReaderPage key={bookId} bookId={bookId} onBack={back} />
  ) : (
    <Shelf onOpen={open} session={session} />
  );
}

function Boot() {
  const [initialization, setInitialization] = useState<
    | { ready: false; failed: boolean }
    | { ready: true; session?: OfflineSession | null }
  >({ ready: false, failed: false });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    setInitialization({ ready: false, failed: false });
    void initializeOfflineSupport()
      .then((session) => {
        if (active) setInitialization({ ready: true, session });
      })
      .catch(() => {
        if (active) setInitialization({ ready: false, failed: true });
      });
    return () => {
      active = false;
    };
  }, [attempt]);

  // Session resolution establishes the offline owner. Keep book requests behind that boundary,
  // while painting an account-neutral shell immediately on slow connections.
  if (!initialization.ready) {
    return (
      <main className="app-boot" aria-busy={!initialization.failed}>
        <Icon name="book" size={34} />
        <header>
          <p className="smallcaps">reading companion</p>
          <h1>Your next chapter.</h1>
        </header>
        <p className="quiet" role={initialization.failed ? "alert" : "status"}>
          {initialization.failed
            ? "Your reading space could not be opened."
            : "Opening your reading space…"}
        </p>
        {initialization.failed && (
          <Button
            variant="primary"
            onClick={() => setAttempt((value) => value + 1)}
          >
            Try again
          </Button>
        )}
      </main>
    );
  }
  return (
    <React.StrictMode>
      <AiRuntimeProvider enabled={initialization.session === undefined}>
        <App session={initialization.session} />
      </AiRuntimeProvider>
    </React.StrictMode>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(<Boot />);
