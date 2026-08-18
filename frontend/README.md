# frontend

React 18 / TypeScript / Vite web reader and companion. The split-pane reader uses vendored foliate-js;
the companion provides a tight orientation, flowing recap, live name cards, and the full-screen Codex.
It also provides Ask the Book: server-bounded answers with claim-level chapter citations and visible
provider usage/cost.
Selected text can be explained, defined, or translated to English without sending other book prose;
the companion also offers an explicit, cited closeout for the latest completed chapter.
The Codex combines a chapter breakdown, roving-tabindex cast list, DOM-native “People & connections”
relationship ledger, and a shared spoiler-time scrubber. The earlier Cytoscape graph was removed.

```bash
cd frontend
npm ci
npm test
npm run build
npm run dev
```

Vite proxies `/api` to `http://127.0.0.1:8000` and sends the same EPUB-safe Content Security Policy
used by the integrated server. Open `http://localhost:5173/?book=<book-id>` after
starting the backend. Accessibility coverage combines jest-axe and role/focus assertions; browser
smokes remain required for `inert` focus restoration and the real reader engine.

## Offline reader

The production build is an installable PWA. The shelf's explicit “Save for offline reading” action
caches that book's manifest and EPUB plus the current position, marks, and preferences. Offline
position and mark changes remain visibly queued and synchronize on the next application open or
`online` event; background execution while the browser is closed is not promised.

Response caches and queued mutations are isolated by owner. Signing out intentionally erases saved
offline books and all owner-scoped reader-local state while retaining the versioned, code-only app
shell. Test service-worker behavior against a production build (`npx vite build` and a static preview),
because Vite's development server does not provide the production offline lifecycle.
