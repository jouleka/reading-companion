#!/usr/bin/env python3
"""Fetch the Standard Ebooks EPUB3 test files (query param avoids the SE
download-counter interstitial; urllib avoids shell glob issues with '?')."""
import os, urllib.request

UA = "reading-companion-spike/0.1 (dev; LIT-4)"
FILES = {
    "books/se-earnest.epub": "https://standardebooks.org/ebooks/oscar-wilde/the-importance-of-being-earnest/downloads/oscar-wilde_the-importance-of-being-earnest.epub?source=download",
    "books/se-pride.epub":   "https://standardebooks.org/ebooks/jane-austen/pride-and-prejudice/downloads/jane-austen_pride-and-prejudice.epub?source=download",
}
os.makedirs("books", exist_ok=True)
for path, url in FILES.items():
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    data = urllib.request.urlopen(req, timeout=90).read()
    open(path, "wb").write(data)
    print(path, len(data), "bytes", "ZIP-OK" if data[:2] == b"PK" else "NOT-ZIP")
