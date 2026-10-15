export type PositionSyncState =
  | "idle"
  | "pending"
  | "saving"
  | "queued"
  | "saved"
  | "conflict"
  | "error";

export type PendingPosition = {
  cfi: string;
  offset: number;
  completedChapter: number;
  positionEpoch: number;
};
type DeliveryResult = "saved" | "conflict" | "queued" | void;

/**
 * A serialized, debounced position outbox. Failed network deliveries remain queued and retry with
 * bounded backoff; a newer relocation replaces an older queued relocation without allowing two
 * writes to race each other.
 */
export class PositionReporter {
  private timer: ReturnType<typeof setTimeout> | undefined;
  private pending: PendingPosition | null = null;
  private completion: PendingPosition | null = null;
  private inFlight = false;
  private active: { report: PendingPosition; generation: number } | null = null;
  private delivery: Promise<void> | null = null;
  private retryAttempt = 0;
  private generation = 0;
  private closed = false;

  constructor(
    private readonly put: (
      cfi: string,
      offset: number,
      completedChapter: number,
      positionEpoch: number,
    ) => DeliveryResult | Promise<DeliveryResult>,
    private readonly delayMs = 700,
    private readonly onState: (state: PositionSyncState) => void = () => {},
    private readonly shouldRetry: (error: unknown) => boolean = () => true,
  ) {}

  schedule(
    cfi: string,
    offset: number,
    completedChapter = 0,
    positionEpoch = 0,
  ): void {
    if (this.closed) return;
    this.pending = { cfi, offset, completedChapter, positionEpoch };
    clearTimeout(this.timer);
    this.onState(this.inFlight ? "queued" : "pending");
    this.timer = setTimeout(() => void this.fire(), this.delayMs);
  }

  /** An explicit final-page affirmation must survive later relocations and network retries. */
  complete(
    cfi: string,
    offset: number,
    completedChapter: number,
    positionEpoch = 0,
  ): void {
    if (this.closed) return;
    this.pending = null;
    this.completion = { cfi, offset, completedChapter, positionEpoch };
    clearTimeout(this.timer);
    this.onState(this.inFlight ? "queued" : "pending");
    void this.fire();
  }

  /** Deliver the pending report now. The keepalive fetch itself may outlive page teardown. */
  flush(): void {
    if ((this.pending || this.completion) && !this.closed) void this.fire();
  }

  /** Persist both the high-water affirmation and the newest cursor before page teardown. */
  retain(persist: (report: Readonly<PendingPosition>) => void): void {
    if (this.closed) return;
    if (this.active?.generation === this.generation) persist(this.active.report);
    if (this.completion) persist(this.completion);
    if (this.pending) persist(this.pending);
  }

  /** A durable successor waits for the current PUT, rather than racing its older cursor. */
  drainAndDispose(
    persist: (report: Readonly<PendingPosition>) => void,
  ): Promise<void> {
    let retentionFailed = false;
    let retentionError: unknown;
    try {
      this.retain(persist);
    } catch (error) {
      retentionFailed = true;
      retentionError = error;
    }
    this.flush();
    const delivery = this.delivery;
    this.dispose();
    const drained = delivery ?? Promise.resolve();
    // Storage may be full or unavailable. Teardown must still stop timers and release the view;
    // report retention failure asynchronously after the best-effort live write has settled.
    return retentionFailed ? drained.then(() => { throw retentionError; }) : drained;
  }

  /** Retry a retained failure immediately, notably after the browser's `online` event. */
  retry(): void {
    if ((!this.pending && !this.completion) || this.closed || this.inFlight)
      return;
    clearTimeout(this.timer);
    this.retryAttempt = 0;
    this.onState("pending");
    this.timer = setTimeout(() => void this.fire(), 0);
  }

  /** Discard pre-reset work and invalidate the result of a write already in flight. */
  cancel(): void {
    this.generation += 1;
    clearTimeout(this.timer);
    this.timer = undefined;
    this.pending = null;
    this.completion = null;
    this.retryAttempt = 0;
    this.onState("idle");
  }

  /** Stop timers after an unmount while allowing a flush's already-started keepalive fetch to run. */
  dispose(): void {
    this.cancel();
    this.closed = true;
  }

  private fire(): Promise<void> {
    clearTimeout(this.timer);
    this.timer = undefined;
    if (this.closed || this.inFlight || (!this.pending && !this.completion))
      return this.delivery ?? Promise.resolve();
    const completing = this.completion !== null;
    const report = this.completion ?? this.pending!;
    if (completing) this.completion = null;
    else this.pending = null;
    this.inFlight = true;
    const generation = this.generation;
    this.active = { report, generation };
    this.onState("saving");
    this.delivery = this.deliver(report, completing, generation);
    return this.delivery;
  }

  private async deliver(
    report: PendingPosition,
    completing: boolean,
    generation: number,
  ): Promise<void> {
    try {
      const result = await this.put(
        report.cfi,
        report.offset,
        report.completedChapter,
        report.positionEpoch,
      );
      if (generation !== this.generation || this.closed) return;
      this.retryAttempt = 0;
      if (this.pending || this.completion) {
        this.onState("pending");
        this.timer = setTimeout(() => void this.fire(), 0);
      } else {
        this.onState(
          result === "conflict"
            ? "conflict"
            : result === "queued"
              ? "queued"
              : "saved",
        );
      }
    } catch (error) {
      if (generation !== this.generation || this.closed) return;
      // A newer relocation is more representative than the failed one; otherwise retain the failure.
      if (completing) this.completion ??= report;
      else this.pending ??= report;
      if (!this.shouldRetry(error)) {
        this.onState("error");
        return;
      }
      const retryMs = Math.min(1_000 * 2 ** this.retryAttempt, 30_000);
      this.retryAttempt += 1;
      this.onState("queued");
      this.timer = setTimeout(() => void this.fire(), retryMs);
    } finally {
      this.inFlight = false;
      this.active = null;
      this.delivery = null;
      // A reset can schedule a new pass while the old request is settling. Its epoch stays bound
      // to that pass, and the physical writes remain serialized even across cancel().
      if (
        !this.closed && generation !== this.generation && (this.pending || this.completion)
      )
        this.timer = setTimeout(() => void this.fire(), 0);
    }
  }
}
