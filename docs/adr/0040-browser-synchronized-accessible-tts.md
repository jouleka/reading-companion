# ADR 0040: Read aloud uses synchronized browser speech with explicit cost boundaries

**Status:** Accepted (2026-07-21)
**Ticket:** LIT-60 / TTS-1

## Context

Read aloud must follow the actual EPUB DOM, move the visible reading place with the spoken word, and
remain usable without configuring a paid AI provider. Sending book prose to the synthesis providers
used by recap features would create a new privacy, tenancy, credential, and variable-cost surface.
Browser speech is broadly available, but a browser or operating system may implement a voice locally
or through its own platform service, so “offline” cannot be inferred merely from the Web Speech API.

## Decision

The reader uses Foliate's existing DOM-aware TTS segmenter to obtain one block of SSML with word marks
at a time. A browser controller converts that markup to plain speech while retaining mark offsets.
`SpeechSynthesis` boundary events feed the matching mark back to Foliate, which selects and scrolls the
exact DOM range. At the end of a block the next block starts automatically; at the end of a loaded
section, the reader advances once and continues only when Foliate exposes a different document.

The visible disclosure provides Play/Pause, Stop, previous/next passage, voice, and 0.5–2× speed using
native controls. Opening another reader modal pauses speech. Leaving the book cancels speech and clears
the synchronized selection. Browser or engine failure produces a generic status and never blocks
ordinary reading.

No Litlet backend or configured AI provider receives text for this feature. The UI therefore states
`Litlet provider cost: $0.00` and explains that there are no Litlet API tokens or paid book-text
requests. A voice is called on-device only when the browser reports `localService=true`; otherwise the
UI says the browser or operating system may use a platform network service. This is a capability and
cost boundary, not a promise about third-party platform behavior.

## Consequences

- Read aloud works in local and hosted readers without a new tenant endpoint, credential grant, or cost
  ledger phase.
- Word synchronization depends on browser boundary-event fidelity. The current word is selected at
  the start even on engines that omit later boundary events.
- Browsers without Web Speech support keep the book readable and expose a truthful unavailable status.
- Paid neural TTS remains out of scope; adding it later requires separate consent, provider, privacy,
  reservation, and measured-cost contracts.
