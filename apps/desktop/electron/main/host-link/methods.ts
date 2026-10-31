/**
 * 宿主工具链路的方法注册表：方法名 → handler。
 *
 * 能力（如内置浏览器）在装配期把自己的方法注册进来，链路客户端只按名字分发，不认识任何业务。
 * handler 自己校验入参（链路只保证 `params` 是 JSON 值），失败抛 `HostMethodError` 给出稳定错误码；
 * 抛其它异常一律按 `INTERNAL` 回给后端。`signal` 在后端撤销这次调用（工具取消或超时）时触发，
 * 长耗时 handler 应据此尽早停下。
 */

export type HostCallMeta = { task_id?: number; tool_call_id?: string };

export type HostMethodHandler = (params: unknown, context: { signal: AbortSignal; meta: HostCallMeta }) => Promise<unknown>;

/** 方法失败；`code` 是后端工具据以解释的稳定错误码。 */
export class HostMethodError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "HostMethodError";
  }
}

export class HostMethodRegistry {
  private readonly handlers = new Map<string, HostMethodHandler>();

  /** 同名重复注册是装配缺陷，直接抛出。 */
  register(method: string, handler: HostMethodHandler): void {
    if (this.handlers.has(method)) throw new Error(`host method already registered: ${method}`);
    this.handlers.set(method, handler);
  }

  get(method: string): HostMethodHandler | undefined {
    return this.handlers.get(method);
  }
}
