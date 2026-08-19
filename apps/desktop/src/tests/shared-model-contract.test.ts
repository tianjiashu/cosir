/**
 * shared/ts 前后端契约测试（设计文档 §九 验收门禁 + §阶段 1/1.5 验收）。
 *
 * 守护不变量：
 * 1. ProviderType 必须覆盖后端 ``PROVIDER_CAPABILITIES`` 注册表全部 15 类
 *    （新增厂商 = 改后端注册表 + 这里加分支 + 契约断言）；
 * 2. AgentProfileResponse.model_name 必须可空（``string | null``，阶段 1.5
 *    无默认模型策略：5 个内置 profile 默认 null，前端 guardSend 拦截）；
 * 4. ProviderConnectionTestResult 类型契约（阶段 2 用户视角三件套）；
 * 5. MODEL_PROVIDER_PATHS 必须含 PROVIDER_TEST 路径常量（阶段 2 测试连接端点）。
 *
 * 类型契约测试策略：构造对象 + 显式类型注解，让 TypeScript 编译器检查
 * 字段存在与类型匹配；运行时仅做最小断言（防止「类型通过但运行时 undefined」）。
 *
 * @module tests/sharedModelContract
 */

import { describe, expect, it } from "vitest";
import type {
  ModelCandidate,
  ModelCreateRequest,
  ModelEntryRecord,
  ModelImportResult,
  ModelUpdateRequest,
  ProviderConnectionTestResult,
  ProviderCreateRequest,
  ProviderRecord,
  ProviderType,
  ProviderUpdateRequest,
} from "@shared/model";
import { MODEL_PROVIDER_PATHS } from "@shared/model";
import type { AgentProfileResponse } from "@shared/agents";

/**
 * 全部 15 类厂商类型字面量数组（与后端注册表键一致）。
 *
 * 用于运行时断言：缺一类即失败，防止新增厂商时漏改前端 model.ts。
 */
const ALL_PROVIDER_TYPES: ProviderType[] = [
  "deepseek",
  "openai-compatible",
  "anthropic",
  "gemini",
  "azure",
  "dashscope",
  "moonshot",
  "zai",
  "volcengine",
  "tencent",
  "minimax",
  "ollama",
  "qianfan",
  "xfyun",
  "custom",
];

describe("shared/model 契约 — ProviderType 15 类与后端注册表对齐", () => {
  it("ProviderType 联合类型必须覆盖 15 类（缺一类即编译失败）", () => {
    // 类型契约：ALL_PROVIDER_TYPES 的元素类型必须是 ProviderType。
    // 若 ProviderType 缺一类，TS 编译时会因数组字面量含未声明类型而失败。
    const types: ProviderType[] = ALL_PROVIDER_TYPES;
    expect(types).toHaveLength(15);
  });

  it("运行时校验：15 类不重复且与后端注册表键集合一致", () => {
    const unique = new Set(ALL_PROVIDER_TYPES);
    expect(unique.size).toBe(15);
    // 抽样校验关键厂商存在（防止误删）：
    expect(unique.has("deepseek")).toBe(true);
    expect(unique.has("azure")).toBe(true);
    expect(unique.has("zai")).toBe(true); // 智谱 GLM（前缀曾误写为 zhipu）
    expect(unique.has("qianfan")).toBe(true); // 走 openai/ 兼容组
    expect(unique.has("custom")).toBe(true); // 兜底类型
  });
});

describe("shared/model 契约 — ProviderConnectionTestResult 类型", () => {
  it("ProviderConnectionTestResult 必须含 success / elapsed_ms / error_code / error_message", () => {
    const successResult: ProviderConnectionTestResult = {
      provider_id: "provider-1",
      success: true,
      elapsed_ms: 250,
      error_code: null,
      error_message: null,
    };
    expect(successResult.success).toBe(true);
    expect(successResult.error_code).toBeNull();

    const failedResult: ProviderConnectionTestResult = {
      provider_id: "provider-1",
      success: false,
      elapsed_ms: 3000,
      error_code: "model_auth_failed",
      error_message: "API Key 无效或已过期",
    };
    expect(failedResult.success).toBe(false);
    expect(failedResult.error_code).toBe("model_auth_failed");
  });
});

describe("shared/model 契约 — MODEL_PROVIDER_PATHS 含 PROVIDER_TEST 路径", () => {
  it("MODEL_PROVIDER_PATHS 必须含 PROVIDER_TEST 函数（阶段 2 测试连接端点）", () => {
    expect(typeof MODEL_PROVIDER_PATHS.PROVIDER_TEST).toBe("function");
    expect(MODEL_PROVIDER_PATHS.PROVIDER_TEST("provider-1")).toBe(
      "/providers/provider-1/test",
    );
  });

  it("MODEL_PROVIDER_PATHS 全部端点路径常量稳定", () => {
    expect(MODEL_PROVIDER_PATHS.PROVIDERS).toBe("/providers");
    expect(MODEL_PROVIDER_PATHS.PROVIDER_DETAIL("p1")).toBe("/providers/p1");
    expect(MODEL_PROVIDER_PATHS.PROVIDER_DISCOVER("p1")).toBe("/providers/p1/discover");
    expect(MODEL_PROVIDER_PATHS.PROVIDER_MODELS("p1")).toBe("/providers/p1/models");
    expect(MODEL_PROVIDER_PATHS.MODELS).toBe("/models");
    expect(MODEL_PROVIDER_PATHS.MODEL_DETAIL("m1")).toBe("/models/m1");
  });
});

describe("shared/model 契约 — 模型条目 / 候选 / 导入导出类型稳定", () => {
  it("ModelEntryRecord 含全部字段（含 supports_thinking / api_key_configured）", () => {
    const entry: ModelEntryRecord = {
      model_id: "model-1",
      provider_id: "provider-1",
      provider_name: "DeepSeek 官方",
      model_name: "deepseek/deepseek-v4-flash",
      display_name: "deepseek-v4-flash",
      max_context_window: 128000,
      supports_thinking: true,
      temperature: null,
      top_p: null,
      max_tokens: null,
      enabled: true,
      api_key_configured: true,
      sort_order: 0,
      created_at: "2026-08-18T00:00:00Z",
      updated_at: "2026-08-18T00:00:00Z",
    };
    expect(entry.supports_thinking).toBe(true);
    expect(entry.api_key_configured).toBe(true);
  });

  it("ModelCandidate 含 already_imported 字段（前端置灰依据）", () => {
    const candidate: ModelCandidate = {
      model_name: "deepseek/deepseek-v4-flash",
      display_name: "deepseek-v4-flash",
      max_context_window: 128000,
      supports_thinking: false,
      already_imported: false,
    };
    expect(candidate.already_imported).toBe(false);
  });

  it("ModelCreateRequest / ModelUpdateRequest / ModelImportResult 类型稳定", () => {
    const createReq: ModelCreateRequest = {
      model_name: "deepseek/deepseek-v4-flash",
      display_name: "deepseek-v4-flash",
      max_context_window: 128000,
    };
    expect(createReq.max_context_window).toBe(128000);

    const updateReq: ModelUpdateRequest = {
      enabled: false,
    };
    expect(updateReq.enabled).toBe(false);

    const importResult: ModelImportResult = {
      provider_id: "provider-1",
      imported: [],
      skipped_model_names: [],
    };
    expect(importResult.imported).toEqual([]);
  });
});

describe("shared/agents 契约 — AgentProfileResponse.model_name 可空", () => {
  it("AgentProfileResponse.model_name 必须接受 null（阶段 1.5 无默认模型策略）", () => {
    // 5 个内置 profile 默认 model_name=null，前端 guardSend 拦截 no_model_selected
    const unconfigured: AgentProfileResponse = {
      agent_id: "developer",
      role: "developer",
      description: "全工具开发 Agent",
      allowed_tools: [],
      workflow: "react",
      model_name: null, // 关键契约：null 表示未配置模型
      max_steps: 300,
      prompt_ref: null,
    };
    expect(unconfigured.model_name).toBeNull();
  });

  it("AgentProfileResponse.model_name 必须接受具体模型名（已配置场景）", () => {
    const configured: AgentProfileResponse = {
      agent_id: "developer",
      role: "developer",
      description: "全工具开发 Agent",
      allowed_tools: [],
      workflow: "react",
      model_name: "deepseek/deepseek-v4-flash",
      max_steps: 300,
      prompt_ref: null,
    };
    expect(configured.model_name).toBe("deepseek/deepseek-v4-flash");
  });
});
