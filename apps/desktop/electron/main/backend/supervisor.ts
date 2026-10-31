import type { ChildProcess } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";

import type { BackendSnapshot, BackendStatus } from "../../shared/host-contract";
import { logDesktopEvent } from "../desktop-log";
import { appLogDir, runtimeDir, systemDataRoot } from "../paths";
import { spawnBackend, stopProcessTree } from "./process";
import { waitForBackend } from "./readiness";
import { resolveBackendRuntime } from "./runtime";

const HOST = "127.0.0.1";
const READY_TIMEOUT_MS = 90_000;
/** 一次启动里，进程起不来或健康检查失败时最多拉起几次。 */
const SPAWN_ATTEMPTS = 3;
/** 一个宿主生命周期内最多自动恢复几次崩溃；用完后由用户显式重启，避免把无限重启伪装成可靠性。 */
const MAX_CRASH_RECOVERY_ATTEMPTS = 1;

/**
 * 后端进程生命周期的唯一所有者：启动、就绪判定、崩溃恢复、重启与停止，并把状态以快照推给订阅方。
 *
 * 并发规则（main 进程单线程，靠下面两条而不是锁）：
 * - `generation` 每次启动、重启、崩溃恢复、停止都递增；异步步骤恢复执行后先比较 generation，
 *   被取代的启动不再发布任何状态，也不会接管后来的进程。
 * - 同一时刻至多一个启动在进行（`starting`）；启动进行中拒绝重启，不叠加。
 *
 * 进程退出只在后端已就绪后才算崩溃；启动阶段的退出由启动流程自己判定并重试。
 */
export class BackendSupervisor {
  private generation = 0;
  // 宿主在应用就绪时立即调用 start()，所以初始即为 starting：页面最早读到的快照不会是会被误判为失败的 stopped。
  private status: BackendStatus = { state: "starting" };
  private backendBaseUrl: string | null = null;
  private child: ChildProcess | null = null;
  private crashRecoveryAttempts = 0;
  private shuttingDown = false;
  private starting: Promise<void> | null = null;
  private readonly listeners = new Set<(snapshot: BackendSnapshot) => void>();

  /** `hostToken` 在宿主进程生命周期内不变，每次拉起后端都注入同一个值。 */
  constructor(private readonly hostToken: string) {}

  snapshot(): BackendSnapshot {
    return { generation: this.generation, status: this.status, backendBaseUrl: this.backendBaseUrl };
  }

  onChange(listener: (snapshot: BackendSnapshot) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** 宿主启动时调用一次；结果只经状态快照发布，不抛出。 */
  start(): Promise<void> {
    this.shuttingDown = false;
    this.generation += 1;
    return this.launch(this.generation);
  }

  /** 用户显式重启：停掉当前进程、重置崩溃恢复次数后重新启动；未能就绪时抛出带原因的错误。 */
  async restart(): Promise<BackendSnapshot> {
    if (this.starting) throw new Error("本地 Agent 后端正在启动，请等待启动完成");
    this.shuttingDown = false;
    this.generation += 1;
    const generation = this.generation;
    this.crashRecoveryAttempts = 0;
    this.publish({ state: "starting" }, null);
    logDesktopEvent("INFO", "backend_supervisor", "backend_restart_requested", { generation });
    // 停旧进程也算在这次启动里：守卫在第一个 await 之前占住，等旧进程退出的宽限期内再来的重启会被拒绝。
    await this.launch(generation, this.releaseChild());
    if (this.status.state !== "ready") {
      throw new Error(this.status.state === "failed" ? this.status.message : "本地 Agent 后端重启失败，请点击重试");
    }
    return this.snapshot();
  }

  /** 宿主退出时调用：停止后端进程树并等它退出，之后不再自动恢复。 */
  async stop(): Promise<void> {
    this.shuttingDown = true;
    this.generation += 1;
    await this.releaseChild();
    this.publish({ state: "stopped" }, null);
    logDesktopEvent("INFO", "backend_supervisor", "backend_stopped", { generation: this.generation });
  }

  /** 后端就绪时的本机地址；退出确认等宿主流程据此决定是否查询后端。 */
  readyBaseUrl(): string | null {
    return this.status.state === "ready" ? this.backendBaseUrl : null;
  }

  /** `before` 是启动前必须先完成的步骤（重启时停旧进程）；守卫在调用时同步占住，覆盖它的耗时。 */
  private launch(generation: number, before: Promise<void> = Promise.resolve()): Promise<void> {
    const run = before
      .then(() => this.launchAttempts(generation))
      .catch((error: unknown) => {
        if (!this.isCurrent(generation)) return;
        const message = error instanceof Error && error.message ? error.message : "本地 Agent 后端启动失败";
        this.publish({ state: "failed", message }, null);
        logDesktopEvent("ERROR", "backend_supervisor", "backend_failed", { generation }, error);
      })
      .finally(() => {
        if (this.starting === run) this.starting = null;
      });
    this.starting = run;
    return run;
  }

  private async launchAttempts(generation: number): Promise<void> {
    if (!this.isCurrent(generation)) return;
    this.publish({ state: "starting" }, null);
    const runtimePath = runtimeDir();
    fs.mkdirSync(runtimePath, { recursive: true });
    const bootstateFile = path.join(runtimePath, "backend.bootstate.json");
    const runtime = await resolveBackendRuntime(runtimePath);
    let lastError = "本地 Agent 后端启动失败";
    for (let attempt = 0; attempt < SPAWN_ATTEMPTS; attempt += 1) {
      if (!this.isCurrent(generation)) return;
      // 上一次运行留下的 failed 状态会让新进程在写入自己的状态前就被误判为失败。
      fs.rmSync(bootstateFile, { force: true });
      const port = await findAvailablePort();
      if (!this.isCurrent(generation)) return;
      const child = spawnBackend({
        runtime,
        port,
        bootstateFile,
        dataDir: systemDataRoot(),
        consoleLogFile: path.join(appLogDir(), "backend-console.log"),
        hostToken: this.hostToken,
      });
      let spawnError: Error | null = null;
      child.once("error", (error) => {
        spawnError = error;
      });
      child.once("exit", (code, signal) => this.handleExit(child, generation, code, signal));
      this.child = child;
      const baseUrl = `http://${HOST}:${port}`;
      const result = await waitForBackend({
        baseUrl,
        bootstateFile,
        timeoutMs: READY_TIMEOUT_MS,
        isAlive: () => spawnError === null && child.exitCode === null && child.signalCode === null,
        isCurrent: () => this.isCurrent(generation),
      });
      if (result.ok) {
        if (!this.isCurrent(generation)) return;
        this.publish({ state: "ready" }, baseUrl);
        logDesktopEvent("INFO", "backend_supervisor", "backend_ready", { generation, port, attempt });
        return;
      }
      if (result.superseded) return;
      lastError = spawnError ? `无法启动本地 Agent 后端：${(spawnError as Error).message}` : result.message;
      logDesktopEvent("WARNING", "backend_supervisor", "backend_start_attempt_failed", { generation, attempt }, lastError);
      if (this.child === child) this.child = null;
      await stopProcessTree(child);
    }
    throw new Error(lastError);
  }

  private handleExit(child: ChildProcess, generation: number, code: number | null, signal: NodeJS.Signals | null): void {
    // 只处理「当前、已就绪的进程」意外退出：主动停止前已把 child 置空，启动阶段的退出由启动流程判定。
    if (this.child !== child || !this.isCurrent(generation) || this.status.state !== "ready") return;
    this.child = null;
    logDesktopEvent("WARNING", "backend_supervisor", "backend_exited", { generation, code, signal });
    if (this.crashRecoveryAttempts < MAX_CRASH_RECOVERY_ATTEMPTS) {
      this.crashRecoveryAttempts += 1;
      this.generation += 1;
      logDesktopEvent("INFO", "backend_supervisor", "backend_crash_recovery_started", { generation: this.generation });
      void this.launch(this.generation);
      return;
    }
    this.publish({ state: "failed", message: "本地 Agent 后端已退出，请点击重试" }, null);
  }

  private async releaseChild(): Promise<void> {
    const child = this.child;
    this.child = null;
    this.backendBaseUrl = null;
    if (child) await stopProcessTree(child);
  }

  private isCurrent(generation: number): boolean {
    return generation === this.generation && !this.shuttingDown;
  }

  private publish(status: BackendStatus, backendBaseUrl: string | null): void {
    this.status = status;
    this.backendBaseUrl = backendBaseUrl;
    const snapshot = this.snapshot();
    for (const listener of this.listeners) listener(snapshot);
  }
}

/** 由系统分配一个空闲端口。分配与后端绑定之间存在极短的竞争窗口，失败时由启动重试兜住。 */
function findAvailablePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, HOST, () => {
      const address = server.address();
      server.close(() => (typeof address === "object" && address ? resolve(address.port) : reject(new Error("无法分配本地后端端口"))));
    });
  });
}
