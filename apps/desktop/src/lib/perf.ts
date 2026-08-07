/**
 * 性能埋点辅助（前端链路耗时追踪）。
 *
 * 统一「用户操作 → 渲染完成」全链路计时：基于 performance.now() 高精度时钟，
 * 所有埋点共享同一 traceId，输出相邻节点相对耗时 + 累计耗时，便于定位慢点。
 *
 * 仅用于排查性能问题；生产环境同样生效（日志经 @lib/logger 落盘 logs/desktop.log）。
 * 打点必须用 logInfo 统一出口，禁止散落 console。
 *
 * @module lib/perf
 */

import { logInfo } from "@/lib/logger";

/**
 * 当前活跃链路追踪实例（模块级单例）。
 *
 * 同一时刻只跑一条用户操作链路（输入指令 → 渲染完成）。链路起点用
 * {@link PerfTrace.startCurrent} 把实例存此处；流式渲染期间的各节点
 * （eventStore / TurnTimeline / ChatPanel）通过 {@link PerfTrace.markCurrent}
 * 复用同一实例，确保整条链路共用同一 traceId 串联，避免各节点在自身时机
 * new PerfTrace 时拿到空 traceId 导致断裂。
 */
let currentTrace: PerfTrace | null = null;

/** 单次链路追踪会话：从起点 launch 起累积多步打点。 */
export class PerfTrace {
  /** 本次链路唯一 ID，用于串联分散的多步日志。 */
  readonly traceId: string;
  /** 链路起点时间戳（performance.now()，毫秒，高精度）。 */
  private readonly startMs: number;
  /** 上一步打点时间戳，用于计算相邻节点相对耗时。 */
  private lastMs: number;

  /**
   * 创建并启动一条性能追踪链路。
   *
   * @param label - 链路名称（如 "user-input-to-render"），用于日志检索。
   * @param traceId - 可选外部 traceId（如 client trace），缺省自动生成。
   *
   * @sideeffect 输出第 0 步日志（链路起点）。
   */
  constructor(label: string, traceId?: string) {
    this.traceId = traceId ?? `perf-${Math.random().toString(36).slice(2, 10)}`;
    this.startMs = performance.now();
    this.lastMs = this.startMs;
    logInfo(`[PERF:${label}] 链路起点`, {
      trace_id: this.traceId,
      phase: "start",
      total_ms: 0,
      step_ms: 0,
    });
  }

  /**
   * 打一个链路节点，记录相对上一步耗时与累计耗时。
   *
   * @param phase - 节点名称（如 "createTurn:post-start"），必须语义唯一可排序。
   * @param extra - 附加上下文（如 turn_id、事件数），用于定位瓶颈。
   *
   * @sideeffect 输出一步性能日志（含 step_ms / total_ms）。
   *
   * @throws 不抛异常。
   */
  mark(phase: string, extra?: Record<string, unknown>): void {
    const now = performance.now();
    const stepMs = now - this.lastMs;
    const totalMs = now - this.startMs;
    this.lastMs = now;
    logInfo(`[PERF] ${phase}`, {
      trace_id: this.traceId,
      phase,
      step_ms: Number(stepMs.toFixed(2)),
      total_ms: Number(totalMs.toFixed(2)),
      ...extra,
    });
  }

  /**
   * 启动并登记当前活跃链路（链路起点的标准入口）。
   *
   * 把实例存模块级 currentTrace，供后续 markCurrent 复用同一 traceId。
   *
   * @param label - 链路名称。
   * @param traceId - 可选外部 traceId（如 client trace），缺省自动生成。
   * @returns 新建的 PerfTrace 实例（调用方可继续 mark）。
   *
   * @sideeffect 覆盖模块级 currentTrace；输出链路起点日志。
   */
  static startCurrent(label: string, traceId?: string): PerfTrace {
    currentTrace = new PerfTrace(label, traceId);
    return currentTrace;
  }

  /**
   * 在当前活跃链路上打点（复用 startCurrent 的同一实例与 traceId）。
   *
   * 用于流式渲染期间的非起点节点：整条链路共用一个 traceId，可 grep 串联。
   * 若无活跃链路（如非用户操作触发的孤立渲染），自动创建匿名链路兜底，
   * 不抛异常、不遗漏日志。
   *
   * @param phase - 节点名称。
   * @param extra - 附加上下文。
   *
   * @sideeffect 输出一步性能日志（含 step_ms / total_ms，基于活跃链路时钟）。
   *
   * @throws 不抛异常。
   */
  static markCurrent(phase: string, extra?: Record<string, unknown>): void {
    if (!currentTrace) {
      currentTrace = new PerfTrace("user-input-to-render");
    }
    currentTrace.mark(phase, extra);
  }

  /**
   * 显式结束当前活跃链路（释放模块级引用，避免长生命周期下的引用滞留）。
   *
   * 调用方在链路语义终点（如渲染完成）调用；不调用也无害（下次 startCurrent 覆盖）。
   *
   * @sideeffect 清空模块级 currentTrace。
   */
  static endCurrent(): void {
    currentTrace = null;
  }
}

