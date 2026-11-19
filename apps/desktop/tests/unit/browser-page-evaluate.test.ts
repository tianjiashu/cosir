import { describe, expect, it } from "vitest";

import { evaluateExpression } from "@/electron/main/browser/page-evaluate";

type Call = [method: string, params: Record<string, unknown>];

/** 只记录命令的 send：求值回一个对象句柄，转 JSON 回固定文本，其余命令成功。 */
function recordingSend(evaluation: unknown = { result: { type: "object", objectId: "value-1" } }) {
  const calls: Call[] = [];
  const send = async (method: string, params: Record<string, unknown>) => {
    calls.push([method, params]);
    if (method === "Runtime.evaluate" || method === "Runtime.callFunctionOn" && params.objectId !== "value-1") return evaluation;
    if (method === "Runtime.callFunctionOn") return { result: { value: '{"ok":true}' } };
    return {};
  };
  return { calls, send };
}

describe("evaluateExpression", () => {
  // 目的：不传元素时行为不变；潜在缺陷：改造后误走 callFunctionOn，或不再释放对象组。
  it("evaluates in the main frame without an element", async () => {
    const { calls, send } = recordingSend();
    const result = await evaluateExpression(send, "document.title", new AbortController().signal);

    expect(result).toEqual({ value: '{"ok":true}', truncated: false, error: null });
    expect(calls[0]).toEqual(["Runtime.evaluate", expect.objectContaining({ expression: "document.title", awaitPromise: true, userGesture: false })]);
    expect(calls.map(([method]) => method)).not.toContain("Runtime.releaseObject");
  });

  // 目的：给了元素时在元素上调用、el 与 this 都是它，结束后释放句柄；潜在缺陷：表达式末尾的行注释吞掉右括号。
  it("runs the expression as a function of the element and releases the element", async () => {
    const { calls, send } = recordingSend();
    await evaluateExpression(send, "el.value // the field", new AbortController().signal, "element-7");

    const [method, params] = calls[0];
    expect(method).toBe("Runtime.callFunctionOn");
    expect(params).toMatchObject({ objectId: "element-7", arguments: [{ objectId: "element-7" }], awaitPromise: true, userGesture: false });
    const declaration = String(params.functionDeclaration);
    // 包装后的函数要能编译：注释在单独一行里结束，不会吞掉收尾的括号。
    expect(() => new Function(`return (${declaration});`)).not.toThrow();
    expect(declaration).toContain("el.value // the field\n");
    expect(calls).toContainEqual(["Runtime.releaseObject", { objectId: "element-7" }]);
  });

  // 目的：iframe 的会话在求值中途分离（框架导航）是脚本的结果；潜在缺陷：当成宿主故障上抛。
  it("reports a frame that went away mid-call as the script's result", async () => {
    const send = async (method: string) => {
      if (method === "Runtime.callFunctionOn") throw new Error("Session with given id not found");
      return {};
    };
    const result = await evaluateExpression(send, "el.value", new AbortController().signal, "element-7");

    expect(result.error).toContain("the page or frame navigated or reloaded meanwhile");
  });
});
