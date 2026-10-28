type AssistantPerformanceReport = {
  /** 本次计时对应的那一轮活动（Run 身份只供诊断）及其结束状态。 */
  activityRunId: number | null;
  activityStatus: string | null;
  frameCount: number;
  mutationCount: number;
  applyFrameTotalMs: number;
  maxApplyFrameMs: number;
  publicationCount: number;
  publicationTotalMs: number;
  maxPublicationMs: number;
  maxItemCount: number;
  reactCommitCount: number;
  reactActualDurationTotalMs: number;
  maxReactActualDurationMs: number;
  canonicalConvergenceCount: number;
  pendingProjectionPublicationCount: number;
  activeRowCommitCount: number;
  historicalRowCommitCount: number;
  maxActiveRowCommitsPerFrame: number;
  composerCommitCount: number;
  terminalConvergenceCount: number;
};

type PerformanceProbeOptions = {
  onReport: (report: AssistantPerformanceReport) => void;
};

type FrameMeasurement = {
  kind: string;
  mutationCount: number;
  durationMs: number;
};

type PublicationMeasurement = {
  durationMs: number;
  itemCount: number;
  activeMessageId: string | null;
  publicationKind: "stream" | "canonical" | "pending";
  animationFrameTimestamp?: number | null;
  terminalConvergence: boolean;
};

type ReactCommitMeasurement = {
  scope: "message-row" | "composer";
  id: string;
  phase: "mount" | "update" | "nested-update";
  actualDurationMs: number;
};

/** 一轮活动结束时的诊断信息：一次计时从活动进入活动态开始，到它进入终态结束。 */
type StreamStatus = {
  activityRunId: number | null;
  activityStatus: string | null;
};

export type AssistantPerformanceProbe = {
  beginStream: () => void;
  finishStream: (status: StreamStatus) => void;
  startTiming: () => number;
  finishTiming: (startedAt: number | null) => number;
  recordFrame: (measurement: FrameMeasurement) => void;
  recordPublication: (measurement: PublicationMeasurement) => void;
  recordTerminalConvergence: () => void;
  recordReactCommit: (measurement: ReactCommitMeasurement) => void;
};

/**
 * 创建一次 task 级的开发期性能探针。
 *
 * 探针累计 Transport 和 React commit 统计，并在一次流结束时通过回调
 * 输出一份聚合结果；它不写入业务状态、不改变渲染时序，也不在 token 粒度打印日志。
 * 生产构建不创建探针（调用方传 null），关闭采集即不创建。调用方负责决定报告落点，例如 Tauri 前端日志。
 */
export function createAssistantPerformanceProbe(options: PerformanceProbeOptions): AssistantPerformanceProbe {
  let lifecycle: "idle" | "active" | "finishing" = "idle";
  let activeMessageId: string | null = null;
  let currentAnimationFrameTimestamp: number | null = null;
  let activeRowCommitsInFrame = 0;
  let report = emptyReport();
  let pendingStatus: StreamStatus | null = null;

  const emitPendingReport = () => {
    if (lifecycle !== "finishing" || pendingStatus === null) return;
    const status = pendingStatus;
    pendingStatus = null;
    lifecycle = "idle";
    options.onReport({
      ...report,
      activityRunId: status.activityRunId,
      activityStatus: status.activityStatus,
    });
  };

  return {
    beginStream: () => {
      emitPendingReport();
      lifecycle = "active";
      activeMessageId = null;
      currentAnimationFrameTimestamp = null;
      activeRowCommitsInFrame = 0;
      report = emptyReport();
    },
    finishStream: (status) => {
      if (lifecycle !== "active") return;
      lifecycle = "finishing";
      pendingStatus = status;
      setTimeout(emitPendingReport, 0);
    },
    startTiming: () => performance.now(),
    finishTiming: (startedAt) => startedAt === null ? 0 : Math.max(0, performance.now() - startedAt),
    recordFrame: (measurement) => {
      if (lifecycle === "idle") return;
      report.frameCount += 1;
      report.mutationCount += measurement.mutationCount;
      report.applyFrameTotalMs += measurement.durationMs;
      report.maxApplyFrameMs = Math.max(report.maxApplyFrameMs, measurement.durationMs);
    },
    recordPublication: (measurement) => {
      if (lifecycle === "idle") return;
      activeMessageId = measurement.activeMessageId;
      const frameTimestamp = measurement.animationFrameTimestamp ?? null;
      if (
        frameTimestamp !== currentAnimationFrameTimestamp
        || measurement.animationFrameTimestamp === undefined
      ) {
        currentAnimationFrameTimestamp = frameTimestamp;
        activeRowCommitsInFrame = 0;
      }
      if (measurement.publicationKind === "canonical" && measurement.terminalConvergence) {
        report.canonicalConvergenceCount += 1;
      }
      if (measurement.publicationKind === "pending") report.pendingProjectionPublicationCount += 1;
      report.publicationCount += 1;
      report.publicationTotalMs += measurement.durationMs;
      report.maxPublicationMs = Math.max(report.maxPublicationMs, measurement.durationMs);
      report.maxItemCount = Math.max(report.maxItemCount, measurement.itemCount);
    },
    recordTerminalConvergence: () => {
      if (lifecycle === "idle") return;
      report.terminalConvergenceCount += 1;
    },
    recordReactCommit: (measurement) => {
      if (lifecycle === "idle") return;
      report.reactCommitCount += 1;
      report.reactActualDurationTotalMs += measurement.actualDurationMs;
      report.maxReactActualDurationMs = Math.max(
        report.maxReactActualDurationMs,
        measurement.actualDurationMs,
      );
      if (measurement.scope === "message-row") {
        if (measurement.id === activeMessageId) {
          report.activeRowCommitCount += 1;
          activeRowCommitsInFrame += 1;
          // 一个 publication 对应一个由 frame store 标记的宿主 WebView 动画帧。
          // 该计数用于确认同一帧没有重复提交 active row。
          report.maxActiveRowCommitsPerFrame = Math.max(
            report.maxActiveRowCommitsPerFrame,
            activeRowCommitsInFrame,
          );
        } else {
          report.historicalRowCommitCount += 1;
        }
      } else {
        report.composerCommitCount += 1;
      }
    },
  };
}

function emptyReport(): AssistantPerformanceReport {
  return {
    activityRunId: null,
    activityStatus: null,
    frameCount: 0,
    mutationCount: 0,
    applyFrameTotalMs: 0,
    maxApplyFrameMs: 0,
    publicationCount: 0,
    publicationTotalMs: 0,
    maxPublicationMs: 0,
    maxItemCount: 0,
    reactCommitCount: 0,
    reactActualDurationTotalMs: 0,
    maxReactActualDurationMs: 0,
    canonicalConvergenceCount: 0,
    pendingProjectionPublicationCount: 0,
    activeRowCommitCount: 0,
    historicalRowCommitCount: 0,
    maxActiveRowCommitsPerFrame: 0,
    composerCommitCount: 0,
    terminalConvergenceCount: 0,
  };
}
