import { describe, it, expect, vi, afterEach } from "vitest";
import {
  logInfo,
  logWarn,
  logError,
  logDebug,
  LogLevel,
} from "@/lib/logger";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("logger 统一出口", () => {
  it("logInfo 不抛错且输出到 console.info", () => {
    const spy = vi.spyOn(console, "info").mockImplementation(() => {});
    expect(() => logInfo("启动", { module: "boot" })).not.toThrow();
    expect(spy).toHaveBeenCalled();
  });

  it("logWarn 不抛错且输出到 console.warn", () => {
    const spy = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(() => logWarn("潜在问题", { module: "x" })).not.toThrow();
    expect(spy).toHaveBeenCalled();
  });

  it("logError 不抛错且输出到 console.error", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => logError("出错了", new Error("boom"), { module: "y" })).not.toThrow();
    expect(spy).toHaveBeenCalled();
  });

  it("logError 能提取错误堆栈（Error 实例）", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const err = new Error("boom");
    logError("网络失败", err, { module: "api", path: "/tasks" });
    // 第三参数是错误堆栈字符串
    const loggedStack = spy.mock.calls[0][2] as string | undefined;
    expect(loggedStack).toBeTypeOf("string");
    expect(loggedStack!.length).toBeGreaterThan(0);
  });

  it("logError 对非 Error 类型 error 转为字符串且不抛错", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => logError("失败", "纯字符串错误", { module: "z" })).not.toThrow();
    expect(spy).toHaveBeenCalled();
  });

  it("logDebug 在 dev 环境输出 console.debug", () => {
    const spy = vi.spyOn(console, "debug").mockImplementation(() => {});
    logDebug("调试信息", { module: "d" });
    // 测试环境 env.DEV=true，应输出
    expect(spy).toHaveBeenCalled();
  });

  it("LogLevel 枚举值正确", () => {
    expect(LogLevel.DEBUG).toBe("debug");
    expect(LogLevel.INFO).toBe("info");
    expect(LogLevel.WARN).toBe("warn");
    expect(LogLevel.ERROR).toBe("error");
  });

  it("非 Tauri 环境下 logError 不触发 invoke 落盘（不抛错）", () => {
    // 测试运行在 node 环境，window 未定义，isTauriEnv() 返回 false
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => logError("落盘路径", new Error("e"))).not.toThrow();
    expect(spy).toHaveBeenCalled();
  });

  it("logError 不泄漏敏感信息（context 敏感字段被脱敏为 [REDACTED]）", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const secretCtx = {
      module: "auth",
      apiKey: "sk-1234567890abcdef",
      password: "hunter2",
      token: "tok-abcdef",
    };
    logError("登录失败", new Error("invalid"), secretCtx);
    const loggedMessage = String(spy.mock.calls[0][0]);
    const loggedContext = JSON.stringify(spy.mock.calls[0][1]);
    // 日志消息本身不应包含 secret 字段值
    expect(loggedMessage).not.toContain("sk-1234567890abcdef");
    expect(loggedMessage).not.toContain("hunter2");
    // context 中的敏感字段应被脱敏为 [REDACTED]，不得出现原文
    expect(loggedContext).not.toContain("sk-1234567890abcdef");
    expect(loggedContext).not.toContain("hunter2");
    expect(loggedContext).toContain("[REDACTED]");
    spy.mockRestore();
  });

  it("logInfo 对嵌套对象递归脱敏", () => {
    const spy = vi.spyOn(console, "info").mockImplementation(() => {});
    logInfo("嵌套", { outer: { secret: "top", keep: "ok" } });
    const ctx = spy.mock.calls[0][1] as Record<string, unknown>;
    const outer = ctx.outer as Record<string, unknown>;
    expect(outer.secret).toBe("[REDACTED]");
    expect(outer.keep).toBe("ok");
  });

  it("logInfo 对数组值逐元素脱敏且保留数组结构", () => {
    const spy = vi.spyOn(console, "info").mockImplementation(() => {});
    logInfo("数组", { items: [{ token: "t1" }, { keep: "v" }], flat: ["a", "b"] });
    const ctx = spy.mock.calls[0][1] as Record<string, unknown>;
    const items = ctx.items as unknown[];
    expect(Array.isArray(items)).toBe(true);
    expect((items[0] as Record<string, unknown>).token).toBe("[REDACTED]");
    expect((items[1] as Record<string, unknown>).keep).toBe("v");
    expect(Array.isArray(ctx.flat)).toBe(true);
    expect(ctx.flat).toEqual(["a", "b"]);
  });

  it("logWarn 对大小写不敏感的敏感键脱敏", () => {
    const spy = vi.spyOn(console, "warn").mockImplementation(() => {});
    logWarn("大小写", { APIKey: "sk-x", Password: "p", normal: "n" });
    const ctx = spy.mock.calls[0][1] as Record<string, unknown>;
    expect(ctx.APIKey).toBe("[REDACTED]");
    expect(ctx.Password).toBe("[REDACTED]");
    expect(ctx.normal).toBe("n");
  });

  it("logDebug 同样脱敏 context（开发期）", () => {
    const spy = vi.spyOn(console, "debug").mockImplementation(() => {});
    logDebug("调试", { apiKey: "sk-x", keep: "ok" });
    const ctx = spy.mock.calls[0][1] as Record<string, unknown>;
    expect(ctx.apiKey).toBe("[REDACTED]");
    expect(ctx.keep).toBe("ok");
  });
});
