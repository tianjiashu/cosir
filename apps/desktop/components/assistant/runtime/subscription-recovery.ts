import { frontendLog } from "@/lib/logging/frontend-log";
import type { TransportIssue } from "@/components/assistant/transport-status";

/** 订阅为什么要重开：意外断开（EOF / 网络 / attach 被拒）计入预算；背压重同步不计。 */
export type ReconnectReason = "disconnected" | "resync_required";

export type SubscriptionRecoveryDeps = {
  taskId: number;
  traceId: string;
  /** 打开（或替换）Task 长订阅；runtime 尚未就绪时为 null。 */
  attach: () => (() => Promise<void>) | null;
  backend: () => { available: boolean; generation: number };
  setIssue: (issue: TransportIssue | null) => void;
};

/**
 * 有界重连只统计「连续」失败：连接稳定保持超过 {@link STABLE_CONNECTION_MS} 后再断开才清零预算，
 * 长时间订阅中偶发的断流不会累积耗尽预算；attach 成功后立刻被关闭（后端异常、代理截断）仍计为
 * 连续失败，不会退化成无延迟的重连死循环。
 */
const MAX_RECONNECT_ATTEMPTS = 5;
const STABLE_CONNECTION_MS = 3_000;
const RECONNECT_DELAYS_MS = [0, 250, 500, 1_000, 2_000] as const;
/** 短暂断流通常在这段时间内就重连成功，只有超过它才向用户提示「连接中断」。 */
const RECOVERY_FEEDBACK_DELAY_MS = 350;

/**
 * 订阅恢复编排（主对话与 Workbench 阅读页共用）：断流重连、背压重同步、手动重新同步与后端恢复都走这里。
 *
 * 订阅跟随 Task、首帧总是完整 canonical state，所以恢复只需重开订阅，不另读 snapshot、不判断
 * Run 状态；业务执行是否继续与订阅无关。计时期间后端换了实例时旧计时器作废，交给后端恢复入口
 * 统一 resync。
 */
export class SubscriptionRecovery {
  private attempts = 0;
  private connectedAt: number | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private feedbackTimer: ReturnType<typeof setTimeout> | null = null;
  private disposed = false;

  public constructor(private readonly deps: SubscriptionRecoveryDeps) {}

  /** 显式重新同步 / 后端不可用：连续失败计数清零，取消待执行的重连与延迟提示。 */
  public resetBudget = (): void => {
    this.attempts = 0;
    this.connectedAt = null;
    this.clearTimers();
  };

  /** 订阅已建立（attach 2xx）：撤下断流提示；预算要等连接稳定后断开才清零。 */
  public markConnected = (): void => {
    this.connectedAt = Date.now();
    this.clearTimers();
  };

  /** 组件卸载或 StrictMode 重放：`active=false` 停掉一切计时器并拒绝后续调度。 */
  public setActive(active: boolean): void {
    this.disposed = !active;
    if (!active) this.clearTimers();
  }

  public reconnect = (reason: ReconnectReason): void => {
    if (this.disposed || !this.deps.backend().available) return;
    if (reason === "resync_required") {
      void frontendLog("INFO", "assistant_transport_resync_required", "Assistant Transport 背压重同步，立即重开订阅", {
        traceId: this.deps.traceId,
        data: { taskId: this.deps.taskId },
      });
      this.resetBudget();
      this.attachNow();
      return;
    }
    if (this.reconnectTimer !== null) return;
    if (this.connectedAt !== null && Date.now() - this.connectedAt >= STABLE_CONNECTION_MS) this.attempts = 0;
    this.connectedAt = null;
    if (this.attempts >= MAX_RECONNECT_ATTEMPTS) {
      void frontendLog("WARNING", "assistant_transport_reconnect_exhausted", "Assistant Transport 重连次数耗尽", {
        traceId: this.deps.traceId,
        data: { taskId: this.deps.taskId, attempts: this.attempts },
      });
      this.deps.setIssue({ message: "本机后端连接多次中断，请重新同步恢复对话。", canResync: true });
      return;
    }
    const attempt = this.attempts;
    this.attempts += 1;
    const delay = RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)];
    const scheduledGeneration = this.deps.backend().generation;
    void frontendLog("INFO", "assistant_transport_reconnect_scheduled", "Assistant Transport 已安排重连", {
      traceId: this.deps.traceId,
      data: { taskId: this.deps.taskId, attempt: attempt + 1, delay },
    });
    if (this.feedbackTimer === null) {
      this.feedbackTimer = setTimeout(() => {
        this.feedbackTimer = null;
        this.deps.setIssue({ message: "连接暂时中断，正在从本机后端恢复最新状态…", canResync: true });
      }, RECOVERY_FEEDBACK_DELAY_MS);
    }
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (scheduledGeneration !== this.deps.backend().generation) return;
      this.attachNow();
    }, delay);
  };

  /** 显式重新同步（「重新同步」按钮）：清零预算后立即重开订阅，首帧即完整状态。 */
  public resync = (issue?: TransportIssue | null): void => {
    void frontendLog("INFO", "assistant_transport_resync", "Assistant Transport 重新同步", {
      traceId: this.deps.traceId,
      data: { taskId: this.deps.taskId, reason: "user_resync" },
    });
    this.resetBudget();
    if (issue !== undefined) this.deps.setIssue(issue);
    this.attachNow();
  };

  /**
   * 后端恢复或换了实例：清零预算并提示正在同步，但不在这里重开——订阅身份跟随后端实例，runtime
   * 已按新身份自行打开；这里再开一次只会把刚建立的连接替换掉。
   */
  public restored = (issue: TransportIssue | null): void => {
    this.resetBudget();
    this.deps.setIssue(issue);
  };

  private attachNow(): void {
    if (this.disposed || !this.deps.backend().available) return;
    void this.deps.attach()?.();
  }

  private clearTimers(): void {
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    if (this.feedbackTimer !== null) clearTimeout(this.feedbackTimer);
    this.reconnectTimer = null;
    this.feedbackTimer = null;
  }
}
