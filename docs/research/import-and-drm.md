# Research: book import, DRM, and reading-position

*Source: research pass, 2026-06-25. What's actually feasible and legal for a third-party reading companion.*

## Headline
Build v1 around **DRM-free EPUB the user already owns** (file import + share-sheet) plus **one-tap public-domain fetch** (Project Gutenberg via Gutendex + Standard Ebooks). Add **Kindle highlights sync via a Readwise-style browser extension on read.amazon.com/notebook** as the one "connect your existing library" hook. **Do NOT import DRM'd purchased books** from Kindle or Apple Books — that's DMCA §1201 circumvention. Live reading position inside Kindle/Apple is unavailable; offer manual position entry.

## 1. Importing DRM'd purchases — don't build it
- **Kindle:** No official full-text export. "Download & Transfer via USB" **removed Feb 26, 2025**; modern books are **KFX** (effectively undecryptable). No official way to get files off the account.
- **Apple Books:** store purchases carry **FairPlay DRM**, no public API for library/position/highlights; sideloaded EPUBs are DRM-free but only retrievable manually on macOS (`~/Library/Containers/com.apple.iBooksX/…`). iOS exposes nothing.
- **Google Play Books / Kobo:** some titles DRM-free; rest use **Adobe DRM (ACSM)**. Not a clean source.
- **Legality:** Stripping DRM violates **DMCA §1201** (no personal-use exception); §1201(a)(2) bans *trafficking* in circumvention tools — exactly what shipping this would be. **Off-limits as a feature.**

## 2. Legitimate sources we CAN use, one-tap
- **Project Gutenberg (~75k):** search/fetch via **Gutendex** JSON API (self-host; MIT). Each result gives direct EPUB + UTF-8 text URLs. Respect the **robot policy** — mirror or rate-limit ≥2s; never per-request scrape gutenberg.org.
- **Standard Ebooks (~1k+, CC0):** beautifully typeset; ingest via per-book GitHub repos or OPDS feed (full feed may be login-gated — verify).
- **EPUB is the universal format.** Parse with EbookLib + BeautifulSoup/lxml — but **EbookLib is AGPL-3.0**, so isolate it or roll a `zipfile`+`lxml` parser. UTF-8 text as fallback.

## 3. Position / highlights
- **Kindle highlights: yes**, via read.amazon.com/notebook (how Readwise/Bookcision work). **Limit:** ~10% publisher clipping cap — a slice, never full text or position.
- **Kindle live position (Whispersync): no public API.** **Apple Books position/highlights: no API** (only a fragile, unsupported macOS SQLite scrape — not shippable). Treat live position as unavailable; capture manually.

## 4. "Beside your book" form factor
- **iPad & Mac side-by-side: viable** (iPadOS 26 windowing + Stage Manager; macOS window tiling). Sandboxing means "companion" = visual adjacency, **not** data access.
- **Browser extension: OK for the Kindle *notebook* (highlights); NOT for book text** — Kindle Cloud Reader renders via HTML5 canvas + obfuscated per-book glyph fonts (DOM scraping → gibberish, and violates Amazon ToS).

## v1 plan
- **Support first:** (a) DRM-free EPUB import (file picker + share sheet); (b) one-tap Gutenberg/Standard Ebooks; (c) optional Kindle-highlights connect (notebook only).
- **Defer:** Apple Books macOS scraping; Google/Kobo ACSM; OCR.
- **Tell users honestly:** we can't import DRM-protected Kindle/Apple purchases (the law forbids removing copy protection) and can't auto-track your page inside those apps. Import the EPUBs you own, grab any classic in one tap, sync Kindle highlights, set your position with a tap.

## Sources
- Kindle USB removal — https://blog.the-ebook-reader.com/2025/02/12/download-transfer-for-kindle-ebooks-going-away-on-february-26/ · https://www.cloudwards.net/news/amazon-removes-usb-download-transfer-kindle-books/
- Apple FairPlay — https://www.epubor.com/what-is-apple-fairplay-drm-and-how-to-get-rid-of-it.html
- DMCA §1201 — https://www.copyright.gov/policy/1201/ · https://www.law.cornell.edu/uscode/text/17/1201
- Gutendex — https://github.com/garethbjohnson/gutendex · PG robot policy — https://www.gutenberg.org/policy/robot_access.html
- Standard Ebooks feeds — https://standardebooks.org/feeds
- Readwise Kindle — https://docs.readwise.io/readwise/docs/importing-highlights/kindle
- Kindle Cloud Reader canvas — https://textmuncher.com/blog/kindle-cloud-reader-copy-not-working
