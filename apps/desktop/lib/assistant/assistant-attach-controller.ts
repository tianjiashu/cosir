export type AssistantAttach = () => Promise<void>;
export type AssistantAttachErrorHandler = (error: unknown, runId: number) => void;

/**
 * Coordinates one readonly task's attach request with the transport hook.
 *
 * A Workbench effect may request an attach before the transport hook has
 * registered its function. This controller retains that request, coalesces
 * duplicate requests while one is running, and leaves failed requests
 * retryable. It owns only in-process subscription coordination; it does not
 * create or persist Run state.
 */
export class AssistantAttachController {
  private attach: AssistantAttach | null = null;
  private requestedRunId: number | null = null;
  private attachedRunId: number | null = null;
  private inFlight: Promise<void> | null = null;

  public constructor(private readonly onError?: AssistantAttachErrorHandler) {}

  public setAttach = (attach: AssistantAttach | null): void => {
    this.attach = attach;
    if (attach && this.requestedRunId !== null) void this.flush();
  };

  public request(runId: number): void {
    if (this.attachedRunId === runId) return;
    this.requestedRunId = runId;
    if (this.attach) void this.flush();
  }

  public cancel(runId?: number): void {
    if (runId === undefined || this.requestedRunId === runId) this.requestedRunId = null;
    if (runId === undefined || this.attachedRunId === runId) this.attachedRunId = null;
  }

  public async waitForIdle(): Promise<void> {
    while (this.inFlight) await this.inFlight;
  }

  private flush(): Promise<void> | undefined {
    if (!this.attach || this.requestedRunId === null || this.inFlight) return;
    const runId = this.requestedRunId;
    const attach = this.attach;
    const operation = Promise.resolve()
      .then(() => attach())
      .then(() => {
        if (this.requestedRunId === runId) this.attachedRunId = runId;
      })
      .catch((error: unknown) => {
        if (this.requestedRunId === runId) this.onError?.(error, runId);
      })
      .finally(() => {
        if (this.inFlight !== operation) return;
        this.inFlight = null;
        if (
          this.attach
          && this.requestedRunId !== null
          && this.requestedRunId !== runId
          && this.attachedRunId !== this.requestedRunId
        ) {
          void this.flush();
        }
      });
    this.inFlight = operation;
    return operation;
  }
}
