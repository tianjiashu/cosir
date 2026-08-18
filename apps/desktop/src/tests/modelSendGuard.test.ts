/**
 * 发送前模型校验纯函数（validateModelSend）路径覆盖测试（设计 §9）。
 *
 * 守护不变量（2026-08-18 起移除 Auto 语义，模型必须显式选择）：
 * 1. 空缓存/未加载拦截（no_models）并引导打开配置中心；
 * 2. 未显式选择模型（selectedModelName=null）拦截（no_model_selected），
 *    仅内联提示、不弹配置中心；
 * 3. 显式选择已不可用模型拦截（model_missing）并引导重新选择；
 * 4. Key 未配置拦截（api_key_missing）：提示前往配置中心填写 API Key（不依赖
 *    Key 的厂商后端恒报 true，天然跳过）；
 * 5. 全部通过放行。
 */
import { describe, expect, it } from "vitest";
import type { ModelEntryRecord } from "@shared/model";
import { validateModelSend, type ModelSendGuardInput } from "@/hooks/useModelSendGuard";

function makeModel(overrides: Partial<ModelEntryRecord> = {}): ModelEntryRecord {
  return {
    model_id: "model-1",
    provider_id: "provider-1",
    provider_name: "DeepSeek 官方",
    model_name: "deepseek/deepseek-v4-flash",
    display_name: "deepseek-v4-flash",
    max_context_window: 128000,
    supports_thinking: false,
    temperature: null,
    top_p: null,
    max_tokens: null,
    enabled: true,
    api_key_configured: true,
    sort_order: 0,
    created_at: "2026-08-17T00:00:00Z",
    updated_at: "2026-08-17T00:00:00Z",
    ...overrides,
  };
}

/** 构造校验输入（默认「缓存就绪 + 显式选择命中模型」的放行基线）。 */
function makeInput(overrides: Partial<ModelSendGuardInput> = {}): ModelSendGuardInput {
  const model = makeModel();
  return {
    availableModels: [model],
    modelsLoaded: true,
    selectedModelName: model.model_name,
    ...overrides,
  };
}

describe("validateModelSend 规则 1：空缓存拦截", () => {
  it("缓存为空列表时拦截并引导打开配置中心", () => {
    const result = validateModelSend(makeInput({ availableModels: [] }));
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.block.reason).toBe("no_models");
      expect(result.block.openSettings).toBe(true);
      expect(result.block.message).toContain("未配置任何模型");
    }
  });

  it("modelsLoaded=false（拉取失败）视同空缓存拦截，不误放行", () => {
    const result = validateModelSend(makeInput({ modelsLoaded: false }));
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.block.reason).toBe("no_models");
    }
  });
});

describe("validateModelSend 规则 2：未选择模型拦截（无 Auto 语义）", () => {
  it("缓存有模型但 selectedModelName=null 时拦截，仅内联提示不弹配置中心", () => {
    const result = validateModelSend(makeInput({ selectedModelName: null }));
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.block.reason).toBe("no_model_selected");
      expect(result.block.openSettings).toBe(false);
      expect(result.block.message).toContain("选择模型");
    }
  });

  it("显式选择命中缓存模型时放行（不再静默回退默认模型）", () => {
    expect(validateModelSend(makeInput()).ok).toBe(true);
  });
});

describe("validateModelSend 规则 3：显式选择模型存在性", () => {
  it("显式选择的模型已不在缓存（删除/禁用）时拦截", () => {
    const result = validateModelSend(
      makeInput({ selectedModelName: "deepseek/deleted-model" }),
    );
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.block.reason).toBe("model_missing");
      expect(result.block.message).toContain("deepseek/deleted-model");
      expect(result.block.openSettings).toBe(true);
    }
  });
});

describe("validateModelSend 规则 4：Key 校验", () => {
  it("目标模型厂商 Key 未配置时拦截，提示前往配置中心填写", () => {
    const model = makeModel({ api_key_configured: false });
    const result = validateModelSend(
      makeInput({ availableModels: [model], selectedModelName: model.model_name }),
    );
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.block.reason).toBe("api_key_missing");
      expect(result.block.openSettings).toBe(true);
      expect(result.block.message).toContain("配置中心");
    }
  });

  it("显式选择且 Key 已配置时放行", () => {
    const model = makeModel({ api_key_configured: true });
    const result = validateModelSend(
      makeInput({ availableModels: [model], selectedModelName: model.model_name }),
    );
    expect(result.ok).toBe(true);
  });
});
