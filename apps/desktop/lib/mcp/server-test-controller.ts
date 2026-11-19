import type { MCPAuthorizationPending, MCPServerConfiguration, MCPServerTestResult } from "@/lib/api/extensions";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";

/**
 * 一个服务（或编辑框里的草稿）此刻的测试进展。主状态（可用、需要授权……）是后端事实，见 `MCPServer.status`；
 * 这里只记界面上的临时进展：测试中、授权中，以及本次的结果。
 */
export type ServerTestState =
  | { phase: "testing" }
  | { phase: "authorizing"; flowId: string | null; host: string | null }
  | { phase: "done"; result: MCPServerTestResult };

/** 测试或授权正在进行：行、编辑框与状态行据此禁用重复操作。 */
export function isTestInProgress(state: ServerTestState | undefined): boolean {
  return state?.phase === "testing" || state?.phase === "authorizing";
}

/** 控制器依赖的后端调用与副作用；hook 里绑定真实接口，测试里换成替身。 */
export type MCPServerTestApi = {
  test: (config: MCPServerConfiguration) => Promise<MCPServerTestResult>;
  authorize: (config: MCPServerConfiguration) => Promise<MCPServerTestResult | MCPAuthorizationPending>;
  wait: (flowId: string) => Promise<MCPServerTestResult>;
  cancel: (flowId: string) => Promise<unknown>;
  openBrowser: (url: string) => void;
  /** 测试或授权结束：后端的本机记录变了，列表里的主状态要重读。 */
  onSettled: () => void;
};

/**
 * 请求本身没拿到后端结果时界面自己给的失败。`needsSignIn`：这次是在登录授权（取消或请求失败），这是界面自己
 * 知道的事实，照样给出「登录授权」让用户重来；普通测试失败没有这个下一步。
 */
const failed = (error: string, needsSignIn = false): MCPServerTestResult => ({
  status: "completed", success: false, tools: [], error, failure_kind: null, needs_sign_in: needsSignIn, agent_available: false,
});

/**
 * 按服务分别推进测试与登录授权：各服务互不阻塞，可以同时测、批量测。
 *
 * 每个键有一个代次：重新测试、授权、取消或清除都会换代，迟到的旧结果（比如取消后才失败返回的等待请求）对不上
 * 代次就丢弃，不会盖掉新状态。键由调用方决定（已保存的服务、编辑框里的草稿各用各的键）。
 *
 * 授权在后端占着一条回调监听，被取代的授权都要通知后端结束：换代时回收进行中的那一个，授权地址迟到
 * （等它的那一代已被取代）时当场取消，不打开浏览器。不经这两处就丢掉授权，监听会挂到后端时限才结束。
 */
export class MCPServerTestController {
  private states: Readonly<Record<string, ServerTestState>> = {};
  private readonly generations = new Map<string, number>();
  private readonly listeners = new Set<() => void>();

  constructor(private readonly api: MCPServerTestApi) {}

  readonly subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  readonly getSnapshot = () => this.states;

  /** 连接测试：从不打开浏览器，需要授权时结果的 `needs_sign_in` 为真。 */
  async test(key: string, config: MCPServerConfiguration): Promise<void> {
    const generation = this.begin(key, { phase: "testing" });
    const result = await this.api.test(config).catch((error: unknown) => failed(userErrorMessage(error, "MCP 连接测试失败")));
    this.settle(key, generation, result);
  }

  /** 并发测试一组服务（全部测试、改变量后重测），各自出结果。 */
  testAll(items: ReadonlyArray<{ key: string; config: MCPServerConfiguration }>): Promise<void> {
    return Promise.all(items.map(({ key, config }) => this.test(key, config))).then(() => undefined);
  }

  /** 用户点了「登录授权」：拿到授权地址立刻交给浏览器打开，再等结果；已有可用令牌时直接出结果。 */
  async authorize(key: string, config: MCPServerConfiguration): Promise<void> {
    const generation = this.begin(key, { phase: "authorizing", flowId: null, host: null });
    try {
      const outcome = await this.api.authorize(config);
      if (outcome.status === "completed") {
        this.settle(key, generation, outcome);
        return;
      }
      if (!this.current(key, generation)) {
        this.release(outcome.flow_id);
        return;
      }
      this.api.openBrowser(outcome.authorization_url);
      this.set(key, { phase: "authorizing", flowId: outcome.flow_id, host: new URL(outcome.authorization_url).host });
      this.settle(key, generation, await this.api.wait(outcome.flow_id));
    } catch (error) {
      this.settle(key, generation, failed(userErrorMessage(error, "MCP 登录授权失败"), true));
    }
  }

  /** 放弃登录授权：界面立刻恢复，后端结束等待、关掉回调监听；正在等的请求随后失败返回会被丢弃。 */
  cancel(key: string): void {
    if (this.states[key]?.phase !== "authorizing") return;
    this.begin(key, { phase: "done", result: failed("已取消授权", true) });
  }

  /** 丢掉一个键的进展（如打开或关闭编辑框时清掉草稿的结果），进行中的授权一并结束。 */
  clear(key: string): void {
    this.supersede(key);
    if (!(key in this.states)) return;
    const rest = { ...this.states };
    delete rest[key];
    this.states = rest;
    this.emit();
  }

  private begin(key: string, state: ServerTestState): number {
    const generation = this.supersede(key);
    this.set(key, state);
    return generation;
  }

  /** 换代：之前那一代的结果作废；它若正在等浏览器授权，通知后端结束。 */
  private supersede(key: string): number {
    const state = this.states[key];
    if (state?.phase === "authorizing" && state.flowId !== null) this.release(state.flowId);
    const generation = (this.generations.get(key) ?? 0) + 1;
    this.generations.set(key, generation);
    return generation;
  }

  /** 结束后端的一次授权（关掉回调监听）。失败可以接受：后端时限会兜底，这里只记日志。 */
  private release(flowId: string): void {
    this.api.cancel(flowId).catch((error: unknown) => {
      void frontendLog("WARNING", "mcp_authorization_cancel_failed", "结束 MCP 登录授权失败", { data: { flowId }, error });
    });
  }

  private settle(key: string, generation: number, result: MCPServerTestResult): void {
    if (!this.current(key, generation)) return;
    this.set(key, { phase: "done", result });
    this.api.onSettled();
  }

  private current(key: string, generation: number): boolean {
    return this.generations.get(key) === generation;
  }

  private set(key: string, state: ServerTestState): void {
    this.states = { ...this.states, [key]: state };
    this.emit();
  }

  private emit(): void {
    for (const listener of this.listeners) listener();
  }
}
