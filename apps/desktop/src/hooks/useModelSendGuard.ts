/**
 * 发送前模型校验（设计文档 §9，D4「前端拦截 + 后端兜底」）。
 *
 * 触发点：发送动作（新 task 走 POST /tasks 前、追加 turn 走
 * POST /tasks/{id}/turns 前）。校验数据源来自本地缓存：
 * - taskStore.availableModels（GET /models：启用模型 + 启用厂商）
 *
 * 校验规则（纯函数 validateModelSend，可独立单测）：
 * 1. 模型缓存为空/未加载 → 拦截（no_models）+ 引导打开配置中心
 * 2. 未显式选择模型（selectedModel=null）→ 拦截（no_model_selected）
 * 3. 显式选择已不可用的模型 → 拦截（model_missing）
 * 4. 目标模型厂商 api_key_configured=false → 拦截（api_key_missing），
 *    引导前往配置中心填写 API Key（不依赖 Key 的厂商后端恒报 true，天然跳过）
 *
 * 注：2026-08-18 起移除「Auto 跟随 Agent 默认」产品语义，模型必须显式选择；
 * 后端 model_name 仍为 Optional（防御兜底，前端保证总是传非 null）。
 *
 * 模型身份以 ``(provider_id, model_name)`` 二元组判定（与后端
 * ``turn_service.create_turn`` 的配对契约一致）：同名不同厂商的模型视为不同模型，
 * 避免跨厂商重名时误判为「仍可用」。
 *
 * @module hooks/useModelSendGuard
 */

import { useCallback } from "react";
import type { ModelEntryRecord } from "@shared/model";
import { findModelBySelection } from "@shared/model";
import { useTaskStore } from "@/stores/taskStore";
import type { SelectedModel } from "@/stores/taskStore";

/** 校验拦截原因（对应设计 §9 四条拦截路径）。 */
export type ModelSendGuardReason =
  | "no_models"
  | "no_model_selected"
  | "model_missing"
  | "api_key_missing";

/** 校验拦截结果（ok=false 时携带）。 */
export interface ModelSendGuardBlock {
  /** 拦截原因。 */
  reason: ModelSendGuardReason;
  /** 面向用户的中文提示文案（内联展示在输入栏）。 */
  message: string;
  /** 是否引导打开模型厂商配置中心（ProviderSettingsDialog）。 */
  openSettings: boolean;
}

/** 校验结果：放行或拦截。 */
export type ModelSendGuardResult = { ok: true } | { ok: false; block: ModelSendGuardBlock };

/** 纯函数校验入参（全部为可快照数据，便于单测）。 */
export interface ModelSendGuardInput {
  /** 可用模型缓存（GET /models）。 */
  availableModels: ModelEntryRecord[];
  /** 缓存是否已成功拉取（false 视同空缓存拦截，见 taskStore 契约）。 */
  modelsLoaded: boolean;
  /** 当前选中模型身份（provider_id + model_name 二元组；null = 未选择）。 */
  selectedModel: SelectedModel | null;
}

/**
 * 按当前选择校验模型可发送性（纯函数，无副作用）。
 *
 * 校验顺序与设计 §9 一致：空缓存 → 未选择模型 → 显式模型存在性 → Key 状态。
 * 产品已移除 Auto 语义：缓存非空时 selectedModel=null 直接拦截（不静默
 * 回退到任何默认模型），保证「用哪个模型」始终是用户显式决策。
 *
 * @param input - 校验输入（模型缓存、加载态、选中模型身份）。
 * @returns 全部通过返回 `{ ok: true }`；否则返回 `{ ok: false, block }`，
 *   block 含拦截原因、用户提示文案与是否引导打开配置中心。
 */
export function validateModelSend(input: ModelSendGuardInput): ModelSendGuardResult {
  const { availableModels, modelsLoaded, selectedModel } = input;

  // 规则 1：空缓存/未加载拦截（未加载视同空，避免拉取失败误放行——taskStore 契约）。
  if (!modelsLoaded || availableModels.length === 0) {
    return {
      ok: false,
      block: {
        reason: "no_models",
        message: "未配置任何模型，请先在配置中心添加模型厂商",
        openSettings: true,
      },
    };
  }

  // 规则 2：未显式选择模型拦截（缓存有模型但 selectedModel=null）。
  if (selectedModel === null) {
    return {
      ok: false,
      block: {
        reason: "no_model_selected",
        message: "请先选择模型再开始对话",
        openSettings: false,
      },
    };
  }

  // 规则 3：显式选择的模型必须仍可用（已删除/禁用的模型不出现在缓存中）。
  // 走共享的二元组匹配：model_name 与 provider_id 同时命中才算同一模型，
  // 避免跨厂商重名（如 openai/gpt-4o 与 azure/gpt-4o）被误判为仍可用。
  const target = findModelBySelection(availableModels, selectedModel);
  if (!target) {
    return {
      ok: false,
      block: {
        reason: "model_missing",
        message: `所选模型（${selectedModel.model_name}）已不可用，请重新选择`,
        openSettings: true,
      },
    };
  }

  // 规则 4：Key 校验。后端语义「不依赖 Key 的厂商恒报 true」，因此 false 必然
  // 意味着该厂商需要 Key 但 providers.api_key 为空（DB 唯一事实来源）。
  if (!target.api_key_configured) {
    return {
      ok: false,
      block: {
        reason: "api_key_missing",
        message: "所选模型的厂商 API Key 未配置，请在配置中心为该厂商填写 API Key",
        openSettings: true,
      },
    };
  }

  return { ok: true };
}

/**
 * 发送前模型校验 Hook。
 *
 * 在发送动作处调用 guardSend()：先确保模型缓存已加载（幂等重试），
 * 再执行纯函数校验。Key 拦截提示为通用文案（引导前往配置中心填写 API Key，
 * DB 是 Key 唯一事实来源，无需反查厂商）。
 *
 * @example
 * ```tsx
 * const { guardSend } = useModelSendGuard();
 * const result = await guardSend();
 * if (!result.ok) {
 *   setGuardMessage(result.block.message);
 *   if (result.block.openSettings) setSettingsOpen(true);
 *   return; // 拦截发送
 * }
 * // ...继续 createTask / createTurn
 * ```
 */
export function useModelSendGuard() {
  const guardSend = useCallback(async (): Promise<ModelSendGuardResult> => {
    // 模型缓存未加载时补拉一次（如启动即发送、上次拉取失败重试）。
    if (!useTaskStore.getState().modelsLoaded) {
      await useTaskStore.getState().refreshAvailableModels();
    }

    const taskState = useTaskStore.getState();
    return validateModelSend({
      availableModels: taskState.availableModels,
      modelsLoaded: taskState.modelsLoaded,
      selectedModel: taskState.selectedModel,
    });
  }, []);

  return { guardSend };
}
