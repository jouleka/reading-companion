import "@fontsource/eb-garamond/400.css";
import "@fontsource/eb-garamond/400-italic.css";
import "@fontsource/eb-garamond/500.css";
import "@fontsource/fraunces/400.css";
import "@fontsource/fraunces/600.css";
import "./styles.css";

import React, { useState } from "react";
import ReactDOM from "react-dom/client";
import { initializeOfflineSupport, type OfflineSession } from "./pwa/offline";
import { ReaderPage } from "./reader/ReaderPage";
import { Shelf } from "./shelf/Shelf";

// Router-less view switch for the shelf and reader.
const params = new URLSearchParams(window.location.search);

function App({ session }: { session?: OfflineSession | null }) {
  const [bookId, setBookId] = useState<string | null>(params.get("book"));
  const open = (id: string) => {
    history.pushState(null, "", `/?book=${id}`);
    setBookId(id);
  };
  const back = () => {
    history.pushState(null, "", "/");
    setBookId(null);
  };
  // key: a book->book transition must fully unmount the old reader (its view/reporter must never
  // route a stale offset into ANOTHER book's permanent bookmark ratchet — pass-2 F4)
  return bookId
    ? <ReaderPage key={bookId} bookId={bookId} onBack={back} />
    : <Shelf onOpen={open} session={session} />;
}

async function boot() {
  const root = ReactDOM.createRoot(document.getElementById("root")!);
  const session = await initializeOfflineSupport();
  root.render(
    <React.StrictMode>
      <App session={session} />
    </React.StrictMode>,
  );
}

boot();
