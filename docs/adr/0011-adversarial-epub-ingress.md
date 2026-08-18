# ADR 0011 — Adversarial EPUB ingress and DRM boundary

**Status:** Accepted (2026-07-13)  
**Ticket:** LIT-11

## Context

An EPUB is a ZIP archive supplied by an untrusted client. The existing parser disabled XML entity
resolution and capped each decompressed read, but the HTTP route read the upload without a bound and
the read helper silently truncated oversized or unreadable members to empty content. Archive-wide
expansion, duplicate paths, traversal-style names, ZIP encryption, and EPUB encryption metadata were
not screened. The segmenter also dropped every OPF `linear="no"` spine item, including items named by
the table of contents.

The product boundary in D9 accepts DRM-free EPUBs and does not implement DRM circumvention. EPUB font
obfuscation is layout packaging, however, and is used by otherwise DRM-free books.

## Decision

Import fails closed before segmentation when any of these bounds or structural checks fail:

- the exact uploaded file defaults to 128 MiB (`EPUB_MAX_UPLOAD_BYTES`); a streaming ASGI guard also
  caps the multipart body at that value plus 64 KiB of form overhead, including chunked requests;
- at most 4,096 ZIP members, 512 MiB declared total uncompressed bytes, and 32 MiB per member;
- parsed container, package, navigation, NCX, and encryption metadata has a tighter 4 MiB read cap;
- an 8 MiB central-directory cap and an allocation-safe record count before Python constructs
  `ZipInfo` objects; split, self-extracting/offset-ambiguous, and ZIP64 archives are rejected because
  the accepted limits do not require those containers;
- for members of at least 1 MiB, a maximum 200:1 declared compression ratio;
- only ZIP stored/deflate methods; no ZIP encryption or symlink entries;
- canonical local POSIX paths only, at most 1,024 UTF-8 bytes, with no traversal, absolute/backslash,
  control-character, duplicate, Unicode-normalized case collision, external URI, or encoded traversal;
- exact-size bounded reads: hitting a cap is an error, never accepted truncation;
- manifest ids must be unique and complete; spine ids and spine documents must resolve.

`META-INF/encryption.xml` is parsed strictly with DTD loading, entity resolution, and network access
disabled. Only IDPF or Adobe font obfuscation is accepted, and only when every cipher target is an
existing `.otf`, `.ttf`, `.woff`, or `.woff2` member. ZIP encryption, unknown encryption algorithms,
encrypted content documents, or malformed encryption metadata are rejected. The API gives the user a
specific DRM-free import message without exposing parser internals.

Imperfect chapter XHTML still uses the existing recovery parser and HTML fallback. This distinction is
intentional: recover readable prose, but reject ambiguous archive identity, resource resolution,
encryption, or resource accounting.

A `linear="no"` spine document referenced by the ToC is retained and classified in spine order. A
non-linear item not referenced by the ToC remains excluded. Both outcomes are emitted as import flags
so this ambiguous publisher signal is auditable rather than silent.

## Consequences

Malformed and hostile inputs stop before source, manifest, memory, or catalog publication. Oversized
but legitimate EPUBs require an explicit upload-policy change and, if they cross a fixed archive
safety bound, a reviewed code-policy change. The fixed decompression limits intentionally prioritize
single-user service availability over accepting every nonconforming publisher archive.

The accepted synthetic suite covers high-ratio bombs, unsafe and duplicate paths, ZIP encryption,
DRM metadata, allowed font obfuscation, missing spine documents, non-linear ToC targets, exact upload
limits, and chunked-body limits. Existing malformed-XHTML and entity-expansion cases remain green.
