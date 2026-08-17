/**
 * 前端日志出口脱敏与键名规范化测试。
 *
 * 覆盖 §3 日志脱敏（fast-redact 路径匹配）与 §4 case 转换（change-case）。
 * 验证敏感字段、嵌套路径、数组元素均被正确脱敏，且非敏感字段与返回结构保持不变。
 *
 * 注意：redactContext 内部会先经 normalizeContextKeys 把 camelCase 键转为 snake_case，
 * 故断言时应使用 snake_case 键名（如 `content_type` 而非 `contentType`）。
 */

import { describe, it, expect } from "vitest";
import { redactContext } from "@/lib/logger";

describe("redactContext (§3 日志脱敏)", () => {
  it("顶层敏感键被脱敏", () => {
    const out = redactContext({ password: "hunter2", token: "abc123", module: "Auth" });
    expect(out?.password).toBe("[REDACTED]");
    expect(out?.token).toBe("[REDACTED]");
    expect(out?.module).toBe("Auth");
  });

  it("嵌套路径（headers.authorization）被通配脱敏", () => {
    const out = redactContext({
      headers: { authorization: "Bearer secret", contentType: "application/json" },
    });
    const headers = out?.headers as Record<string, unknown>;
    expect(headers.authorization).toBe("[REDACTED]");
    // 非敏感嵌套字段保留（键名已转 snake_case）
    expect(headers.content_type).toBe("application/json");
  });

  it("数组中的对象元素被逐元素脱敏", () => {
    const out = redactContext({
      items: [{ password: "a" }, { token: "b" }],
    });
    const items = out?.items as Array<Record<string, unknown>>;
    expect(items[0].password).toBe("[REDACTED]");
    expect(items[1].token).toBe("[REDACTED]");
  });

  it("非敏感字段原样保留且保持对象结构", () => {
    const out = redactContext({ module: "Foo", taskId: "t1", durationMs: 42 });
    expect(out?.module).toBe("Foo");
    expect(out?.task_id).toBe("t1");
    expect(out?.duration_ms).toBe(42);
    expect(typeof out).toBe("object");
  });

  it("不修改调用方持有的原对象", () => {
    const original = { password: "secret", keep: "v" };
    redactContext(original);
    expect(original.password).toBe("secret");
    expect(original.keep).toBe("v");
  });

  it("空上下文返回 undefined", () => {
    expect(redactContext({})).toBeUndefined();
  });
});

describe("toSnakeCase (§4 case 转换)", () => {
  it("camelCase 转 snake_case", () => {
    const out = redactContext({ myFieldName: "v" });
    expect(out).toBeDefined();
    expect("myFieldName" in (out as Record<string, unknown>)).toBe(false);
    expect((out as Record<string, unknown>).my_field_name).toBe("v");
  });

  it("acronym 边界（XMLParser / version2Update）转 snake_case 不回归", () => {
    // 注意：change-case 的 snakeCase 对数字后接大写的行为是 `version2_update`
    // （数字2 后不加下划线），此处按库的真实输出断言，避免臆测边界。
    const out = redactContext({ XMLParser: "v", version2Update: "v" });
    expect((out as Record<string, unknown>).xml_parser).toBe("v");
    expect((out as Record<string, unknown>).version2_update).toBe("v");
  });

  it("连字符与空格转为下划线", () => {
    const out = redactContext({ "my-Field Name": "v" });
    expect((out as Record<string, unknown>)["my_field_name"]).toBe("v");
  });
});
