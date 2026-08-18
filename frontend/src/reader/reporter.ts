export type PositionSyncState =
  | "idle"
  | "pending"
  | "saving"
  | "queued"
  | "saved"
  | "conflict"
  | "error";

type PendingPosition = { cfi: string; offset: number; completedChapter: number };
type DeliveryResult = "saved" | "conflict" | "queued" | void;

/**
 * A serialized, debounced position outbox. Failed network deliveries remain queued and retry with
 * bounded backoff; a newer relocation replaces an older queued relocation without allowing two
 * writes to race each other.
 */
export class PositionReporter {
  private timer: ReturnType<typeof setTimeout> | undefined;
  private pending: PendingPosition | null = null;
  private inFlight = false;
  private retryAttempt = 0;
  private generation = 0;
  private closed = false;

  constructor(
    private readonly put: (
      cfi: string,
      offset: number,
      completedChapter: number,
    ) => DeliveryResult | Promise<DeliveryResult>,
    private readonly delayMs = 700,
    private readonly onState: (state: PositionSyncState) => void = () => {},
    private readonly shouldRetry: (error: unknown) => boolean = () => true,
  ) {}

  schedule(cfi: string, offset: number, completedChapter = 0): void {
    if (this.closed) return;
    this.pending = { cfi, offset, completedChapter };
    clearTimeout(this.timer);
    this.onState(this.inFlight ? "queued" : "pending");
    this.timer = setTimeout(() => void this.fire(), this.delayMs);
  }

  /** Deliver the pending report now. The keepalive fetch itself may outlive page teardown. */
  flush(): void {
    if (this.pending && !this.closed) void this.fire();
  }

  /** Retry a retained failure immediately, notably after the browser's `online` event. */
  retry(): void {
    if (!this.pending || this.closed || this.inFlight) return;
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
    this.inFlight = false;
    this.retryAttempt = 0;
    this.onState("idle");
  }

  /** Stop timers after an unmount while allowing a flush's already-started keepalive fetch to run. */
  dispose(): void {
    this.cancel();
    this.closed = true;
  }

  private async fire(): Promise<void> {
    clearTimeout(this.timer);
    this.timer = undefined;
    if (this.closed || this.inFlight || !this.pending) return;
    const report = this.pending;
    this.pending = null;
    this.inFlight = true;
    const generation = this.generation;
    this.onState("saving");
    try {
      const result = await this.put(report.cfi, report.offset, report.completedChapter);
      if (generation !== this.generation || this.closed) return;
      this.inFlight = false;
      this.retryAttempt = 0;
      if (this.pending) {
        this.onState("pending");
        this.timer = setTimeout(() => void this.fire(), 0);
      } else {
        this.onState(result === "conflict" ? "conflict" : result === "queued" ? "queued" : "saved");
      }
    } catch (error) {
      if (generation !== this.generation || this.closed) return;
      this.inFlight = false;
      // A newer relocation is more representative than the failed one; otherwise retain the failure.
      this.pending ??= report;
      if (!this.shouldRetry(error)) {
        this.onState("error");
        return;
      }
      const retryMs = Math.min(1_000 * 2 ** this.retryAttempt, 30_000);
      this.retryAttempt += 1;
      this.onState("queued");
      this.timer = setTimeout(() => void this.fire(), retryMs);
    }
  }
}
