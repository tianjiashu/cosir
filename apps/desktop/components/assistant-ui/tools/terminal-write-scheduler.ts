/**
 * 串行调度 xterm 写入工作。
 *
 * xterm 的 write 是异步解析操作。调用方可以在解析期间重复 request，调度器
 * 只保留一个待处理标记，等当前写入完成后再让调用方基于最新累计快照计算动作。
 * 该模块不理解终端文本、ANSI 或 Transport，只负责生命周期和背压边界。
 */
export class TerminalWriteScheduler {
  private requested = false;
  private running = false;
  private disposed = false;

  constructor(
    private readonly flush: (done: () => void) => void,
    private readonly onError: (error: unknown) => void,
  ) {}

  get isRunning(): boolean {
    return this.running;
  }

  request(): void {
    if (this.disposed) return;
    this.requested = true;
    this.drain();
  }

  dispose(): void {
    this.disposed = true;
    this.requested = false;
  }

  private drain(): void {
    if (this.disposed || this.running || !this.requested) return;

    this.requested = false;
    this.running = true;
    let completed = false;
    const done = () => {
      if (completed) return;
      completed = true;
      this.running = false;
      this.drain();
    };

    try {
      this.flush(done);
    } catch (error) {
      done();
      this.onError(error);
    }
  }
}
