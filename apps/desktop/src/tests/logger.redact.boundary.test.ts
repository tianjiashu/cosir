/**
 * 前端日志脱敏（§3）/ case 转换（§4）的**边界补充测试**。
 *
 * 现有 `logger.test.ts` 已覆盖顶层/嵌套/数组的基本脱敏，本文件专补开发规范
 * 要求但原测试缺失的边界：
 * - `redactContext(undefined)` 空值路径；
 * - 深层嵌套 + 数组嵌数组的任意深度；
 * - 返回值必须是**对象而非字符串**（fast-redact 默认 serialize 会返回字符串，
 *   本实现 serialize:false 必须保持对象结构，此契约一旦回归将导致日志上下文损坏）；
 * - 大对象不栈溢出；
 * - 敏感键的 camelCase 形态（apiKey/privateKey/accessKey）归一化后确被命中；
 * - 敏感值确为字面量 "[REDACTED]"（而非 undefined/删除键）；
 * - 敏感键为非字符串值（对象/数组/null）时的处理。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { redactContext, logError, logWarn, logInfo } from "@/lib/logger";

describe("redactContext 空值与返回类型契约（§3 边界）", () => {
  // 测试目的：undefined 上下文不应抛异常，应短路返回 undefined。
  // 可能发现的缺陷：对 undefined 直接展开/取键导致 TypeError，使日志调用点崩溃。
  it("undefined 上下文返回 undefined 且不抛异常", () => {
    expect(() => redactContext(undefined)).not.toThrow();
    expect(redactContext(undefined)).toBeUndefined();
  });

  // 测试目的：验证 serialize:false 生效 —— 返回对象而非 JSON 字符串。
  // 可能发现的缺陷：误用 fast-redact 默认 serialize:true，导致 LogEntry.context
  // 变成字符串，落盘 JSONL 字段结构损坏、后端查询契约被破坏。
  it("返回值是对象而非序列化字符串（serialize:false 契约）", () => {
    const out = redactContext({ password: "p", module: "M" });
    expect(typeof out).toBe("object");
    expect(out).not.toBeNull();
    expect(Array.isArray(out)).toBe(false);
    expect(typeof out).not.toBe("string");
    // 对象可直接按键读取（字符串形态下 out?.module 会是 undefined）。
    expect(out?.module).toBe("M");
  });

  // 测试目的：嵌套层的返回值同样必须是对象，而非被序列化的字符串。
  // 可能发现的缺陷：递归脱敏中某层被 JSON 化，深层字段不可读。
  it("嵌套层同样保持对象结构", () => {
    const out = redactContext({ outer: { inner: { token: "t", keep: 1 } } });
    const outer = out?.outer as Record<string, unknown>;
    expect(typeof outer).toBe("object");
    const inner = outer.inner as Record<string, unknown>;
    expect(typeof inner).toBe("object");
    expect(inner.token).toBe("[REDACTED]");
    expect(inner.keep).toBe(1);
  });

  // 测试目的：脱敏后的值必须是字面量 "[REDACTED]"，而非删除键或置 undefined。
  // 可能发现的缺陷：censor 配置漂移（如改成 "***"）会让下游按 [REDACTED] 断言的
  // 排查流程/审计失效；键被删除则丢失「此处曾有敏感字段」的线索。
  it("敏感值被替换为字面量 [REDACTED] 且键仍存在", () => {
    const out = redactContext({ secret: "s" }) as Record<string, unknown>;
    expect("secret" in out).toBe(true);
    expect(out.secret).toBe("[REDACTED]");
    expect(out.secret).not.toBeUndefined();
  });
});

describe("redactContext 深层嵌套与数组（§3 边界）", () => {
  // 测试目的：任意深度（5 层）嵌套对象中的敏感键都应被脱敏。
  // 可能发现的缺陷：只做单层或固定深度脱敏，深层 secret 泄漏到日志文件。
  it("5 层深嵌套中的敏感键被脱敏", () => {
    const out = redactContext({
      l1: { l2: { l3: { l4: { l5: { password: "deep", keep: "ok" } } } } },
    });
    const l5 = (
      ((((out?.l1 as Record<string, unknown>).l2 as Record<string, unknown>)
        .l3 as Record<string, unknown>).l4 as Record<string, unknown>).l5
    ) as Record<string, unknown>;
    expect(l5.password).toBe("[REDACTED]");
    expect(l5.keep).toBe("ok");
  });

  // 测试目的：数组嵌数组、数组内嵌对象再嵌数组的混合结构均需递归脱敏。
  // 可能发现的缺陷：仅对数组第一层做 map，二维数组内的 token 泄漏。
  it("二维数组与数组内深层对象均被脱敏", () => {
    const out = redactContext({
      matrix: [[{ token: "a" }], [{ nested: { api_key: "b" } }]],
    });
    const matrix = out?.matrix as Array<Array<Record<string, unknown>>>;
    expect(matrix[0][0].token).toBe("[REDACTED]");
    const nested = matrix[1][0].nested as Record<string, unknown>;
    expect(nested.api_key).toBe("[REDACTED]");
  });

  // 测试目的：标量数组元素与 null 元素不应被破坏或引发异常。
  // 可能发现的缺陷：递归中未判空导致 null 触发 TypeError；标量被转成 {}。
  it("数组中的标量与 null 元素原样保留", () => {
    const out = redactContext({ list: [1, "s", null, true] });
    expect(out?.list).toEqual([1, "s", null, true]);
  });

  // 测试目的：敏感键的值本身是对象/数组时也应整体被 censor 替换，而非下钻保留内部值。
  // 可能发现的缺陷：把 {token: {value: "x"}} 的内部 value 原样留下，等于未脱敏。
  it("敏感键的值为对象时整体被脱敏，不泄漏内部值", () => {
    const out = redactContext({ token: { value: "leak-me" } });
    expect(JSON.stringify(out)).not.toContain("leak-me");
    expect(out?.token).toBe("[REDACTED]");
  });

  // 测试目的：大对象（1000 键 + 200 层数组）不应栈溢出或超时。
  // 可能发现的缺陷：递归实现对大结构性能不可接受或 RangeError。
  it("大对象脱敏不栈溢出", () => {
    const big: Record<string, unknown> = { password: "p" };
    for (let i = 0; i < 1000; i += 1) big[`field${i}`] = i;
    big.arr = Array.from({ length: 200 }, (_, i) => ({ idx: i, secret: "s" }));
    let out: Record<string, unknown> | undefined;
    expect(() => {
      out = redactContext(big);
    }).not.toThrow();
    expect(out?.password).toBe("[REDACTED]");
    expect((out?.arr as Array<Record<string, unknown>>)[199].secret).toBe("[REDACTED]");
    expect((out?.arr as Array<Record<string, unknown>>)[199].idx).toBe(199);
  });

  // 测试目的：深层对象也不得污染调用方持有的原始引用（原测试只验了顶层）。
  // 可能发现的缺陷：redactor 非 deep 模式变异传入对象，业务对象里的真实 token
  // 被替换成 [REDACTED]，导致后续请求带错凭证（隐蔽的功能性 bug）。
  it("不修改原对象的深层引用", () => {
    const inner = { password: "real-secret", keep: "v" };
    const original = { outer: { inner }, items: [{ token: "real-token" }] };
    redactContext(original);
    expect(inner.password).toBe("real-secret");
    expect(original.outer.inner.password).toBe("real-secret");
    expect(original.items[0].token).toBe("real-token");
  });
});

describe("redactContext 敏感键命中面（§3 + §4 联动）", () => {
  // 测试目的：camelCase 敏感键（apiKey/privateKey/accessKey）经 snake_case 归一化后
  // 必须命中 SENSITIVE_PATHS，否则源码注释声称的「只列 snake_case」策略是错的。
  // 可能发现的缺陷：归一化与匹配表不一致，导致 apiKey 明文写入日志。
  it("camelCase 敏感键归一化后被脱敏", () => {
    const out = redactContext({
      apiKey: "k",
      privateKey: "pk",
      accessKey: "ak",
      Authorization: "bearer",
    }) as Record<string, unknown>;
    expect(out.api_key).toBe("[REDACTED]");
    expect(out.private_key).toBe("[REDACTED]");
    expect(out.access_key).toBe("[REDACTED]");
    expect(out.authorization).toBe("[REDACTED]");
  });

  // 测试目的：覆盖 SENSITIVE_PATHS 全表，确保每个声明的键都真实生效（无死配置）。
  // 可能发现的缺陷：某敏感键拼写错误或未编译进 redactor，形成「看似已脱敏」的假象。
  it("SENSITIVE_PATHS 全表逐项生效", () => {
    const keys = [
      "api_key",
      "password",
      "token",
      "secret",
      "authorization",
      "access_key",
      "private_key",
      "credential",
      "cookie",
    ];
    for (const key of keys) {
      const out = redactContext({ [key]: "leak-me" }) as Record<string, unknown>;
      expect(out[key], `敏感键 ${key} 未被脱敏`).toBe("[REDACTED]");
    }
  });

  // 测试目的：非敏感但名字相近的键（token_count / password_hint_shown）不应被误脱敏，
  // 避免过度脱敏破坏可排查性。
  // 可能发现的缺陷：使用了模糊/前缀匹配，把诊断字段一起抹掉，日志失去排查价值。
  it("名字相近的非敏感键不被误脱敏", () => {
    const out = redactContext({
      tokenCount: 42,
      passwordHintShown: true,
      secretsCount: 3,
    }) as Record<string, unknown>;
    expect(out.token_count).toBe(42);
    expect(out.password_hint_shown).toBe(true);
    expect(out.secrets_count).toBe(3);
  });
});

describe("日志出口的失败路径可排查性（§3 落地校验）", () => {
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.spyOn(console, "info").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // 测试目的：logError 必须把 module 上下文与堆栈一起输出，保证异常可定位模块。
  // 可能发现的缺陷：context 被丢弃或 stack 未提取，线上异常无法定位到模块。
  it("logError 输出 module 上下文与堆栈", () => {
    const err = new Error("boom");
    logError("操作失败", err, { module: "TestModule", taskId: "t-1" });
    const call = (console.error as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    expect(String(call[0])).toContain("[ERROR]");
    expect(String(call[0])).toContain("boom");
    const ctx = call[1] as Record<string, unknown>;
    expect(ctx.module).toBe("TestModule");
    expect(ctx.task_id).toBe("t-1");
    expect(typeof call[2]).toBe("string"); // stack
  });

  // 测试目的：logError 的上下文同样要脱敏（错误路径往往携带请求头/凭证）。
  // 可能发现的缺陷：错误分支绕过脱敏，事故现场日志泄漏 token。
  it("logError 上下文中的敏感字段被脱敏", () => {
    logError("请求失败", new Error("e"), {
      module: "M",
      headers: { authorization: "Bearer leak-me" },
    });
    const call = (console.error as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    expect(JSON.stringify(call[1])).not.toContain("leak-me");
  });

  // 测试目的：logWarn 同样携带 module 上下文（降级/失败告警需可定位）。
  // 可能发现的缺陷：无上下文的裸告警，排查时无法判断来源模块。
  it("logWarn 输出 module 上下文", () => {
    logWarn("降级处理", { module: "WarnModule", reason: "timeout" });
    const call = (console.warn as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    expect(String(call[0])).toContain("[WARN]");
    const ctx = call[1] as Record<string, unknown>;
    expect(ctx.module).toBe("WarnModule");
    expect(ctx.reason).toBe("timeout");
  });

  // 测试目的：logError 传入非 Error 值时不应崩溃，且消息中含字符串化结果。
  // 可能发现的缺陷：对 error.message 无条件取值导致二次异常，吞掉原始错误。
  it("logError 接受非 Error 值不抛异常", () => {
    expect(() => logError("非 Error", "plain-string-error", { module: "M" })).not.toThrow();
    const call = (console.error as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    expect(String(call[0])).toContain("plain-string-error");
    expect(call[2]).toBeUndefined(); // 非 Error 无 stack
  });

  // 测试目的：logInfo 也需经脱敏出口（正常路径同样可能带 cookie）。
  // 可能发现的缺陷：仅 error 分支脱敏，info 分支明文泄漏。
  it("logInfo 上下文中的敏感字段被脱敏", () => {
    logInfo("完成", { module: "M", cookie: "leak-me" });
    const call = (console.info as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    expect(JSON.stringify(call[1])).not.toContain("leak-me");
  });
});

describe("logError 错误因果链展开（§6 日志铁律：堆栈不丢失）", () => {
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.spyOn(console, "info").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // 测试目的：当错误经 ServiceError 等包装、底层 ky HTTPError 挂在 .cause 上时，
  // 落盘堆栈必须包含底层原始错误的类型、消息与堆栈，不能只保留顶层。
  // 可能发现的缺陷：logError 仅取顶层 error.stack，底层 ky 错误堆栈丢失，
  // 违反「错误日志必须有堆栈」铁律，线上无法定位根因。
  it("logError 保留底层 cause 链的堆栈", () => {
    const root = new Error("Request failed with status 504");
    root.name = "HTTPError";
    const wrapped = new Error("API call failed");
    (wrapped as Error & { cause?: unknown }).cause = root;

    logError("操作失败", wrapped, { module: "ApiClient" });
    const call = (console.error as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    const stack = call[2] as string;
    expect(typeof stack).toBe("string");
    // 顶层堆栈仍在
    expect(stack).toContain("API call failed");
    // 底层 cause 链被拼接进堆栈
    expect(stack).toContain("Caused by 1. HTTPError: Request failed with status 504");
    expect(stack).toContain("Request failed with status 504");
  });

  // 测试目的：多层 cause（A→B→C）应全部展开，且顺序自上而下。
  // 可能发现的缺陷：递归只展开一层，深层根因被丢弃。
  it("logError 展开多层 cause 链", () => {
    const c = new Error("disk full");
    c.name = "IOError";
    const b = new Error("write failed");
    (b as Error & { cause?: unknown }).cause = c;
    const a = new Error("flush failed");
    (a as Error & { cause?: unknown }).cause = b;

    logError("持久化失败", a, { module: "Store" });
    const call = (console.error as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    const stack = call[2] as string;
    expect(stack).toContain("Caused by 1. Error: write failed");
    expect(stack).toContain("Caused by 2. IOError: disk full");
  });

  // 测试目的：顶层 error 无 cause 时，stack 仍是顶层堆栈且不含空段。
  // 可能发现的缺陷：formatCauseChain 对空链返回多余换行/分隔符污染日志。
  it("无 cause 时堆栈不含 Caused by 段", () => {
    logError("单纯的错", new Error("solo"), { module: "M" });
    const call = (console.error as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    const stack = call[2] as string;
    expect(stack).toContain("solo");
    expect(stack).not.toContain("Caused by");
  });

  // 测试目的：cause 链出现循环引用（a.cause=b, b.cause=a）不得无限递归或崩溃。
  // 可能发现的缺陷：while 循环无限递归导致栈溢出，使原始错误日志也写不出。
  it("循环 cause 链不无限递归", () => {
    const a = new Error("a");
    const b = new Error("b");
    (a as Error & { cause?: unknown }).cause = b;
    (b as Error & { cause?: unknown }).cause = a;

    expect(() => logError("循环", a, { module: "M" })).not.toThrow();
    const call = (console.error as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    const stack = call[2] as string;
    expect(stack).toContain("Caused by 1. Error: b");
    expect(stack).not.toContain("Caused by 2.");
  });
});
