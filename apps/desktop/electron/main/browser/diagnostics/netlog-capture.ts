import fs from "node:fs";
import path from "node:path";

import { logDesktopEvent } from "../../desktop-log";
import { appLogDir } from "../../paths";
import { browserPartitionSession } from "../browser-partition";

const MAX_DURATION_MS = 15 * 60 * 1000;
const MAX_BYTES = 20 * 1024 * 1024;
const FLUSH_TIMEOUT_MS = 5_000;

/** 一次采集：开始即落盘，到时限、到大小或被停止时收尾；收尾只发生一次。 */
type Run = { file: string; ready: Promise<boolean>; timer: ReturnType<typeof setInterval> | null; stopping: Promise<void> | null };

/**
 * 内置浏览器分区的 Chromium 原生 NetLog，由用户在复现问题时手动开启（应用菜单「诊断」）：CDP 日志只看到请求的
 * 开始与结束，挂住的请求卡在代理、DNS、连接池还是握手，只有这里有。同一时刻至多一次采集，每次写一个带时间的新文件
 * （`browser-netlog-<时间>.json`，不覆盖上一次），最多 15 分钟、文件到 20 MiB 停止（每秒检查一次，允许一个间隔的增量）。
 *
 * `default` 模式不采 Cookie 与认证值，但 URL 仍可能含参数：文件只留本机（权限 0600 由 Chromium 设定），
 * 不作为工具结果或附件返回。采集失败只记日志，不影响宿主；退出时经 `stop` 收尾，至多等 5 秒。
 */
export class BrowserNetLogCapture {
  private run: Run | null = null;

  /** `onChange`：采集开始或结束（含到限自停）时调用，菜单据此刷新可用项。 */
  constructor(private readonly onChange: () => void) {}

  get active(): boolean {
    return this.run !== null;
  }

  start(): void {
    if (this.run !== null) return;
    const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15);
    const run: Run = { file: path.join(appLogDir(), `browser-netlog-${stamp}.json`), ready: Promise.resolve(false), timer: null, stopping: null };
    // 先登记再开始：开始失败时 `begin` 经 `stop` 收尾，要能找到这次采集。
    this.run = run;
    run.ready = this.begin(run);
    this.onChange();
  }

  /** 定时收尾、用户停止与应用退出共用同一个 Promise；没有采集时立即完成。 */
  stop(): Promise<void> {
    const run = this.run;
    if (run === null) return Promise.resolve();
    if (run.stopping !== null) return run.stopping;
    let deadline: ReturnType<typeof setTimeout>;
    const timeout = new Promise<never>((_resolve, reject) => { deadline = setTimeout(() => reject(new Error("network log flush timed out")), FLUSH_TIMEOUT_MS); });
    const netLog = browserPartitionSession().netLog;
    run.stopping = Promise.race([run.ready.then(async (started) => {
      this.clearTimer(run);
      if (!started) return;
      if (netLog.currentlyLogging) await netLog.stopLogging();
      logDesktopEvent("INFO", "browser", "browser_netlog_stopped", { file: run.file });
    }), timeout])
      .catch((error: unknown) => logDesktopEvent("WARNING", "browser", "browser_netlog_stop_failed", { file: run.file }, error))
      .finally(() => {
        clearTimeout(deadline);
        this.clearTimer(run);
        if (this.run === run) this.run = null;
        this.onChange();
      });
    return run.stopping;
  }

  /** 返回是否真的开始了；开始失败时记下原因并结束这次采集。 */
  private async begin(run: Run): Promise<boolean> {
    const netLog = browserPartitionSession().netLog;
    try {
      fs.mkdirSync(path.dirname(run.file), { recursive: true });
      // 当前 Electron 的 maxFileSize 模式实测报 ERR_INSUFFICIENT_RESOURCES；使用原生流式模式，时间与大小由这里约束。
      await netLog.startLogging(run.file, { captureMode: "default" });
    } catch (error) {
      logDesktopEvent("WARNING", "browser", "browser_netlog_start_failed", { file: run.file }, error);
      void this.stop();
      return false;
    }
    logDesktopEvent("INFO", "browser", "browser_netlog_started", { file: run.file, max_bytes: MAX_BYTES, duration_ms: MAX_DURATION_MS });
    const deadline = Date.now() + MAX_DURATION_MS;
    run.timer = setInterval(() => {
      let stop = Date.now() >= deadline || !netLog.currentlyLogging;
      try { stop ||= fs.statSync(run.file).size >= MAX_BYTES; }
      catch { stop = true; }
      if (stop) void this.stop();
    }, 1_000);
    run.timer.unref();
    return true;
  }

  private clearTimer(run: Run): void {
    if (run.timer !== null) clearInterval(run.timer);
    run.timer = null;
  }
}
