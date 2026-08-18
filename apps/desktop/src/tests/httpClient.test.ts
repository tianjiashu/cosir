// @vitest-environment happy-dom
/**
 * httpClient 单元测试。
 *
 * 目标：验证 ky 实例 `apiClient` 的错误归一逻辑与 retry 配置。
 * - beforeError 调用纯函数 normalizeToServiceError，本测试直接对该纯函数做断言
 *   （避免触发真实 ky 运行时与网络请求）；同时验证 hooks.beforeError 确实调用它。
 * - 因 httpClient.ts 模块加载时会执行 ky.create()，此处必须 mock "ky" 模块，
 *   但错误归一逻辑用的是 ky 的 HTTPError / isHTTPError 类型守卫，通过构造
 *   `name === "HTTPError"` 的普通对象即可模拟（ky 的 isHTTPError 亦支持该判定），
 *   无需真实 ky 实例。
 *
 * @module tests/httpClient
 */

import { afterEach, describe, expect, it, vi } from "vitest";

// vi.hoisted：在 vi.mock 工厂被 hoist 之前初始化，供工厂闭包引用，避免
// "Cannot access ... before initialization"（hoisting）错误。
const h = vi.hoisted(() => ({
  mockBeforeErrorHooks: [] as Array<(e: unknown) => Promise<unknown>>,
}));

// 必须在导入被测模块前 mock ky：提供 create（返回携带 hooks 的伪实例）与类型守卫。
vi.mock("ky", () => {
  // 模拟 HTTPError / NetworkError / TimeoutError 的类型守卫：ky 用 name 或 instanceof 判定。
  class HTTPError extends Error {
    name = "HTTPError";
    response: { status: number; headers: Headers };
    data?: unknown;
    constructor(response: { status: number; headers?: Headers }, data?: unknown) {
      super(`Request failed with ${response.status}`);
      this.response = { status: response.status, headers: response.headers ?? new Headers() };
      this.data = data;
    }
  }
  class NetworkError extends Error {
    name = "NetworkError";
  }
  class TimeoutError extends Error {
    name = "TimeoutError";
  }
  const isHTTPError = (e: unknown): e is HTTPError =>
    e instanceof HTTPError || (e as { name?: string })?.name === "HTTPError";

  const kyDefault = Object.assign(
    function ky() {
      return {} as unknown;
    },
    {
      create: (opts: { hooks?: { beforeError?: typeof h.mockBeforeErrorHooks } }) => {
        if (opts?.hooks?.beforeError) {
          h.mockBeforeErrorHooks.push(...opts.hooks.beforeError);
        }
        return { __isApiClient: true, options: opts } as unknown;
      },
    },
  );

  return {
    default: kyDefault,
    HTTPError,
    NetworkError,
    TimeoutError,
    isHTTPError,
  };
});

import { normalizeToServiceError, apiClient } from "@/services/httpClient";
import { ServiceError } from "@/services/types";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("normalizeToServiceError - HTTP 错误归一", () => {
  it("HTTP 404 且 body.detail 存在：statusCode=404、message=detail", () => {
    // 测试目的：HTTP 错误归一的核心契约——statusCode 取自 response.status，
    // message 取自 body.detail；可能发现缺陷：statusCode 被置 0 / message 回退失败。
    const err = {
      name: "HTTPError",
      message: "Request failed with 404",
      response: { status: 404, headers: new Headers() },
      data: { detail: "not found" },
    };
    const se = normalizeToServiceError(err);
    expect(se).toBeInstanceOf(ServiceError);
    expect(se.statusCode).toBe(404);
    expect(se.message).toBe("not found");
    // cause 应保留原始 ky 错误，便于上层日志排障（不暴露给 UI）。
    expect(se.cause).toBe(err);
  });

  it("HTTP 500 且 body.detail 存在：statusCode=500、message=detail", () => {
    // 测试目的：非 404 的 5xx 同样正确归一；可能发现缺陷：仅处理了特定状态码。
    const err = {
      name: "HTTPError",
      message: "Request failed with 500",
      response: { status: 500, headers: new Headers() },
      data: { detail: "internal error" },
    };
    const se = normalizeToServiceError(err);
    expect(se.statusCode).toBe(500);
    expect(se.message).toBe("internal error");
  });

  it("HTTP 错误无 detail（data 为空对象）：message 回退到 error.message", () => {
    // 测试目的：body 不含 detail 时回退 message；可能发现缺陷：回退到空串或 undefined。
    const err = {
      name: "HTTPError",
      message: "Request failed with 403",
      response: { status: 403, headers: new Headers() },
      data: {},
    };
    const se = normalizeToServiceError(err);
    expect(se.statusCode).toBe(403);
    expect(se.message).toBe("Request failed with 403");
  });

  it("HTTP 错误 data 为 null：message 回退到 error.message", () => {
    // 测试目的：data 为 null（空响应体）时的回退；可能发现缺陷：对 null 取 .detail 抛错。
    const err = {
      name: "HTTPError",
      message: "Request failed with 422",
      response: { status: 422, headers: new Headers() },
      data: null,
    };
    expect(() => normalizeToServiceError(err)).not.toThrow();
    const se = normalizeToServiceError(err);
    expect(se.statusCode).toBe(422);
    expect(se.message).toBe("Request failed with 422");
  });

  it("HTTP 错误 detail 为空字符串：不视为有效，回退到 error.message", () => {
    // 测试目的：detail 为 "" 时不应覆盖 message（代码用 length>0 判定）；
    // 可能发现缺陷：空串被当作有效 detail。
    const err = {
      name: "HTTPError",
      message: "Request failed with 400",
      response: { status: 400, headers: new Headers() },
      data: { detail: "" },
    };
    const se = normalizeToServiceError(err);
    expect(se.statusCode).toBe(400);
    expect(se.message).toBe("Request failed with 400");
  });

  it("HTTP 错误 detail 为非字符串（数字）：回退到 error.message", () => {
    // 测试目的：detail 类型非字符串时不污染 message；
    // 可能发现缺陷：将数字转字符串拼进 message 或抛类型错误。
    const err = {
      name: "HTTPError",
      message: "Request failed with 409",
      response: { status: 409, headers: new Headers() },
      data: { detail: 12345 },
    };
    const se = normalizeToServiceError(err);
    expect(se.statusCode).toBe(409);
    expect(se.message).toBe("Request failed with 409");
  });

  it("HTTP 错误 data 不是对象（字符串）：回退到 error.message", () => {
    // 测试目的：data 被解析成纯文本（非 JSON 对象）时不应取 .detail；
    // 可能发现缺陷："detail" in string 误判或抛错。
    const err = {
      name: "HTTPError",
      message: "Request failed with 502",
      response: { status: 502, headers: new Headers() },
      data: "raw text body",
    };
    const se = normalizeToServiceError(err);
    expect(se.statusCode).toBe(502);
    expect(se.message).toBe("Request failed with 502");
  });
});

describe("normalizeToServiceError - 非 HTTP 错误（网络/超时）", () => {
  it("NetworkError（非 isHTTPError）：statusCode=0、message=error.message", () => {
    // 测试目的：网络层异常归一为 statusCode=0；可能发现缺陷：误判为 HTTP 取不到 status 抛错。
    const err = new Error("Failed to fetch");
    err.name = "NetworkError";
    const se = normalizeToServiceError(err);
    expect(se).toBeInstanceOf(ServiceError);
    expect(se.statusCode).toBe(0);
    expect(se.message).toBe("Failed to fetch");
    expect(se.cause).toBe(err);
  });

  it("TimeoutError（非 isHTTPError）：statusCode=0、message=error.message", () => {
    // 测试目的：超时异常归一为 statusCode=0；可能发现缺陷：timeout 未被归类为网络层。
    const err = new Error("Request timed out");
    err.name = "TimeoutError";
    const se = normalizeToServiceError(err);
    expect(se.statusCode).toBe(0);
    expect(se.message).toBe("Request timed out");
  });

  it("普通 Error（非 ky 错误）：statusCode=0、message=error.message", () => {
    // 测试目的：兜底路径——任何非 HTTP 的未知错误都归一为 statusCode=0；
    // 可能发现缺陷：非 HTTPError 分支漏处理导致返回原始错误而非 ServiceError。
    const err = new Error("something blew up");
    const se = normalizeToServiceError(err);
    expect(se.statusCode).toBe(0);
    expect(se.message).toBe("something blew up");
    expect(se).toBeInstanceOf(ServiceError);
  });

  it("非 Error 的抛出值（字符串）：statusCode=0、message 为 String(error)", () => {
    // 测试目的：极端兜底——throw "boom" 这类非对象错误不崩溃；
    // 可能发现缺陷：对字符串取 .message 抛 TypeError。
    const se = normalizeToServiceError("boom");
    expect(se.statusCode).toBe(0);
    expect(se.message).toBe("boom");
    expect(se).toBeInstanceOf(ServiceError);
  });
});

describe("apiClient - ky v2 配置项迁移护栏", () => {
  /**
   * 回归护栏：ky v2 把 `prefixUrl` 重命名为 `prefix`；若模块仍用旧键名，
   * 配置静默被忽略，所有相对路径请求会落到页面 origin 根而非 `/api` 前缀，
   * 导致 `GET /agents`、`GET /workspaces`、`GET /models` 等全部失败。
   *
   * 测试手段：读取 mock ky.create 挂载的 options，断言存在 `prefix` 键且值为
   * 期望的基准路径；同时断言不存在已被废弃的 `prefixUrl` 键（防止回退）。
   */
  it("使用 ky v2 的 `prefix` 键注入基础路径，而非废弃的 `prefixUrl`", () => {
    // 测试目的：锁定 ky v2 破坏性重命名，防止相对路径请求因前缀缺失而全部失败；
    // 可能发现缺陷：仍写 prefixUrl 导致请求落到错误路径（前端日志大量 404/解析失败）。
    const options = (apiClient as unknown as { options: Record<string, unknown> }).options;
    expect("prefix" in options).toBe(true);
    expect(options["prefix"]).toBe("");
    expect("prefixUrl" in options).toBe(false);
  });
});

describe("apiClient - beforeError 钩子接线与配置", () => {
  it("apiClient 已注册 beforeError 钩子，且钩子调用 normalizeToServiceError", async () => {
    // 测试目的：验证 ky.create 的 hooks.beforeError 真实调用了归一纯函数
    // （端到端验证接线，而非只测纯函数本身）；可能发现缺陷：钩子未注册/未调用。
    expect(h.mockBeforeErrorHooks.length).toBeGreaterThanOrEqual(1);
    const hook = h.mockBeforeErrorHooks[0]!;
    const kyErr = {
      name: "HTTPError",
      message: "Request failed with 404",
      response: { status: 404, headers: new Headers() },
      data: { detail: "not found" },
    };
    const result = await hook(kyErr);
    expect(result).toBeInstanceOf(ServiceError);
    expect((result as ServiceError).statusCode).toBe(404);
    expect((result as ServiceError).message).toBe("not found");
  });

  it("retry.methods 不含 POST（POST 不重试），含 GET/DELETE/PUT/HEAD/OPTIONS", () => {
    // 测试目的：验证 POST 不在重试方法列表（避免非幂等写操作被重复提交），
    // GET/DELETE 等幂等方法在列；可能发现缺陷：POST 被加入重试导致重复创建任务。
    const options = (apiClient as unknown as { options: { retry?: { methods?: string[] } } }).options;
    const methods = options.retry?.methods ?? [];
    expect(methods).not.toContain("post");
    expect(methods).toContain("get");
    expect(methods).toContain("delete");
    expect(methods).toContain("put");
    expect(methods).toContain("head");
    expect(methods).toContain("options");
  });

  it("retry.limit=2 且 throwHttpErrors=true", () => {
    // 测试目的：验证重试次数与错误抛出开关符合预期；
    // 可能发现缺陷：throwHttpErrors 误关导致非 2xx 不归一为 ServiceError。
    const options = (apiClient as unknown as {
      options: { retry?: { limit?: number }; throwHttpErrors?: boolean };
    }).options;
    expect(options.retry?.limit).toBe(2);
    expect(options.throwHttpErrors).toBe(true);
  });
});

/**
 * H3 回归护栏：DELETE / 级联删除不得被 ky 的「5xx 重试」重放。
 *
 * 修复形态：`retry.statusCodes: []`（关闭一切基于 HTTP 状态码的重试）
 * + `retry.shouldRetry: (error) => !isHTTPError(error)`（仅在请求根本没送达的
 * 网络/超时错误时重试）。二者缺一，deleteWorkspace/deleteTask 这类破坏性
 * 级联删除会在后端 500/504（实际已执行成功但响应超时）时被重放，造成二次副作用。
 */
describe("apiClient - H3：级联删除不被 5xx 重试重放", () => {
  /** 读取 apiClient 的 retry 配置（mock 的 ky.create 把 opts 挂在 .options 上）。 */
  function retryOptions(): {
    limit?: number;
    methods?: string[];
    statusCodes?: number[];
    shouldRetry?: (error: unknown) => boolean;
  } {
    const options = (apiClient as unknown as {
      options: {
        retry?: {
          limit?: number;
          methods?: string[];
          statusCodes?: number[];
          shouldRetry?: (error: unknown) => boolean;
        };
      };
    }).options;
    return options.retry ?? {};
  }

  it("retry.statusCodes 为空数组：关闭一切基于 HTTP 状态码的重试", () => {
    // 测试目的：锁死 H3 修复的第一道闸门——状态码重试白名单必须为空。
    // 可能发现缺陷：若回退为 ky 默认（[408,413,429,500,502,503,504]），
    //   DELETE 遇后端 500/504 会被重放，级联删除执行两次（不可逆数据破坏）。
    const retry = retryOptions();
    expect(retry.statusCodes).toBeDefined();
    expect(Array.isArray(retry.statusCodes)).toBe(true);
    expect(retry.statusCodes).toEqual([]);
    expect(retry.statusCodes).toHaveLength(0);
  });

  it("shouldRetry 已配置且为函数", () => {
    // 测试目的：第二道闸门必须存在（statusCodes 为空时仍需 shouldRetry 兜住
    //   ky 内部其它重试触发路径）；可能发现缺陷：修复只改了 statusCodes 漏了 shouldRetry。
    const retry = retryOptions();
    expect(typeof retry.shouldRetry).toBe("function");
  });

  it("shouldRetry(HTTPError 500)：返回 false —— 送达后的 5xx 绝不重试", () => {
    // 测试目的：H3 的核心断言——请求已送达（后端可能已完成删除）且返回 5xx 时，
    //   必须放弃重试。可能发现缺陷：shouldRetry 写成 isHTTPError(error)（漏取反）
    //   或恒返回 true，导致 DELETE 被重放造成二次破坏性副作用。
    const shouldRetry = retryOptions().shouldRetry!;
    const httpErr500 = {
      name: "HTTPError",
      message: "Request failed with 500",
      response: { status: 500, headers: new Headers() },
    };
    expect(shouldRetry(httpErr500)).toBe(false);
  });

  it("shouldRetry(HTTPError 502/503/504)：一律返回 false（含网关/超时类 5xx）", () => {
    // 测试目的：覆盖 ky 默认重试白名单里的全部 5xx 状态码，逐一确认被关闭；
    // 可能发现缺陷：只对 500 做了特判，504（网关超时，级联删除最常见的重放触发点）漏网。
    const shouldRetry = retryOptions().shouldRetry!;
    for (const status of [500, 502, 503, 504]) {
      const err = {
        name: "HTTPError",
        message: `Request failed with ${status}`,
        response: { status, headers: new Headers() },
      };
      // 精确断言 false（而非 falsy），避免返回 undefined 时被弱断言放过。
      expect(shouldRetry(err), `status ${status} 不应重试`).toBe(false);
    }
  });

  it("shouldRetry(HTTPError 408/413/429)：一律返回 false（ky 默认白名单里的 4xx）", () => {
    // 测试目的：ky 默认对 408/413/429 也会重试；确认这些同样被关闭，
    //   否则 429 限流下的 DELETE 仍会被重放。
    // 可能发现缺陷：修复只覆盖 5xx，遗漏 4xx 重试白名单。
    const shouldRetry = retryOptions().shouldRetry!;
    for (const status of [408, 413, 429]) {
      const err = {
        name: "HTTPError",
        message: `Request failed with ${status}`,
        response: { status, headers: new Headers() },
      };
      expect(shouldRetry(err), `status ${status} 不应重试`).toBe(false);
    }
  });

  it("shouldRetry(HTTPError 404)：返回 false —— 4xx 无重试价值", () => {
    // 测试目的：普通 4xx 同样不重试（幂等但无意义，且会放大日志噪声）；
    // 可能发现缺陷：判定逻辑写成「仅 5xx 不重试」，4xx 反而被重试。
    const shouldRetry = retryOptions().shouldRetry!;
    const err = {
      name: "HTTPError",
      message: "Request failed with 404",
      response: { status: 404, headers: new Headers() },
    };
    expect(shouldRetry(err)).toBe(false);
  });

  it("shouldRetry(TimeoutError)：返回 true —— 非 HTTPError 视为未送达，可重试", () => {
    // 测试目的：修复不得过度收紧成「一律不重试」，否则弱网下 GET 失去韧性。
    // 可能发现缺陷：shouldRetry 写成恒 false，网络抖动时所有请求一次性失败。
    const shouldRetry = retryOptions().shouldRetry!;
    const err = new Error("Request timed out");
    err.name = "TimeoutError";
    expect(shouldRetry(err)).toBe(true);
  });

  it("shouldRetry(NetworkError)：返回 true —— 连接层失败可安全重试", () => {
    // 测试目的：网络不可达（请求未送达后端，无副作用）时保留重试；
    // 可能发现缺陷：把 NetworkError 也误判为 HTTPError 导致失去重试韧性。
    const shouldRetry = retryOptions().shouldRetry!;
    const err = new Error("Failed to fetch");
    err.name = "NetworkError";
    expect(shouldRetry(err)).toBe(true);
  });

  it("shouldRetry(普通 Error / 无 name 对象)：返回 true（非 HTTPError 兜底分支）", () => {
    // 测试目的：兜底分支语义——只要不是 HTTPError 就按「未送达」处理；
    // 可能发现缺陷：类型守卫对无 name 字段的对象抛异常（shouldRetry 内部崩溃）。
    const shouldRetry = retryOptions().shouldRetry!;
    expect(shouldRetry(new Error("boom"))).toBe(true);
    expect(() => shouldRetry({})).not.toThrow();
    expect(shouldRetry({})).toBe(true);
  });

  it("双闸门联合语义：statusCodes 为空 + shouldRetry 对全部 HTTP 状态码为 false", () => {
    // 测试目的：把 H3 的完整不变式压成一条断言——不存在任何「HTTP 响应已返回」
    //   却仍触发重试的状态码。可能发现缺陷：某个状态码被开后门特判。
    const retry = retryOptions();
    expect(retry.statusCodes).toEqual([]);
    const shouldRetry = retry.shouldRetry!;
    const allStatuses = [400, 401, 403, 404, 408, 409, 410, 413, 422, 429, 500, 501, 502, 503, 504];
    const retried = allStatuses.filter((status) =>
      shouldRetry({
        name: "HTTPError",
        message: `Request failed with ${status}`,
        response: { status, headers: new Headers() },
      }),
    );
    expect(retried).toEqual([]);
  });

  it("DELETE 仍在 retry.methods 中，但只对未送达错误生效（不构成重放风险）", () => {
    // 测试目的：说明「delete 留在 methods 里」是安全的——闸门在 shouldRetry/statusCodes，
    //   而非方法白名单。可能发现缺陷：误以为需要移除 delete 而破坏了弱网韧性，
    //   或反之保留了状态码重试。
    const retry = retryOptions();
    expect(retry.methods).toContain("delete");
    const shouldRetry = retry.shouldRetry!;
    // 已送达的 5xx：不重试（无重放）
    expect(
      shouldRetry({
        name: "HTTPError",
        message: "Request failed with 500",
        response: { status: 500, headers: new Headers() },
      }),
    ).toBe(false);
    // 未送达的网络错误：可重试（保留韧性）
    const netErr = new Error("Failed to fetch");
    netErr.name = "NetworkError";
    expect(shouldRetry(netErr)).toBe(true);
  });
});
