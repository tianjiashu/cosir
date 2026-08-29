/**
 * 已配置厂商清单持久化 store（Zustand）。
 *
 * 单一职责：承载「用户已配置的厂商清单」的本地持久化与内存态，不混入
 * 其他职责（不负责厂商创建/编辑的网络请求，不渲染 UI）。
 *
 * 设计背景（§3.5）：后端无 GET /providers 端点，厂商配置中心不再拉取列表，
 * 而是将创建/编辑/删除成功的回参写入本 store 持久化（localStorage 键
 * ``coding-agent.configuredProviders``），对话框直接读本 store 渲染「已配置厂商清单」，
 * 刷新/重启后不丢失、不依赖任何后端列表端点。
 *
 * 持久化单出口模式：所有写 localStorage 的动作都经本 store 的 action 完成，避免
 * 各处分散直写导致内存态与持久化态撕裂（参照 taskStore 的 persistSelectedModel 范式）。
 *
 * @module stores/providerConfigStore
 */

import { create } from "zustand";
import type { ProviderRecord } from "@shared/model";
import { logWarn } from "@/lib/logger";

/** 已配置厂商清单的本地持久化键（刷新/重启不丢失）。 */
const CONFIGURED_PROVIDERS_STORAGE_KEY = "coding-agent.configuredProviders";

/**
 * 从 localStorage 读取已配置厂商清单。
 *
 * 解析失败或不可用（隐私模式）时记录 WARN 日志并返回空数组，不影响主流程。
 *
 * @returns 持久化的厂商记录列表；无有效值时返回空数组。
 */
function loadConfiguredProviders(): ProviderRecord[] {
  try {
    const raw = localStorage.getItem(CONFIGURED_PROVIDERS_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (Array.isArray(parsed)) {
      // 仅保留形似 ProviderRecord 的对象（防御脏数据），不影响后续渲染。
      return parsed.filter(
        (item): item is ProviderRecord =>
          item !== null && typeof item === "object" && typeof (item as ProviderRecord).provider_id === "number",
      );
    }
    return [];
  } catch (err) {
    logWarn("读取已配置厂商清单失败，降级为空", {
      module: "providerConfigStore",
      error: err instanceof Error ? err.message : String(err),
    });
    return [];
  }
}

/**
 * 将已配置厂商清单写入 localStorage（唯一出口）。
 *
 * localStorage 不可用时记录 WARN 日志，不抛错、不影响内存态。
 *
 * @param providers - 待持久化的厂商记录列表。
 */
function persistConfiguredProviders(providers: ProviderRecord[]): void {
  try {
    localStorage.setItem(CONFIGURED_PROVIDERS_STORAGE_KEY, JSON.stringify(providers));
  } catch (err) {
    logWarn("持久化已配置厂商清单失败", {
      module: "providerConfigStore",
      error: err instanceof Error ? err.message : String(err),
    });
  }
}

/** providerConfigStore 状态接口。 */
interface ProviderConfigState {
  /** 用户已配置的厂商清单（本地全量，含未启用/未配 Key）。 */
  configuredProviders: ProviderRecord[];
}

/** providerConfigStore 动作接口。 */
interface ProviderConfigActions {
  /**
   * 重置整份清单为给定列表（新建/批量刷新后全量覆盖）。
   *
   * 所有写 localStorage 的路径统一走本 action，确保内存态与持久化态一致。
   *
   * @param providers - 全量厂商记录列表。
   */
  setConfiguredProviders: (providers: ProviderRecord[]) => void;
  /**
   * 新增或替换一个已配置厂商（创建/编辑成功后回参写入）。
   *
   * 以 provider_id 为唯一键去重：已存在则原地替换，不存在则置顶插入。
   *
   * @param provider - 待写入的厂商记录（来自后端创建/编辑回参）。
   */
  upsertConfiguredProvider: (provider: ProviderRecord) => void;
  /**
   * 从清单中移除一个已配置厂商（删除成功后调用）。
   *
   * @param providerId - 待移除的厂商标识（后端 int 主键）。
   */
  removeConfiguredProvider: (providerId: number) => void;
}

/**
 * 已配置厂商清单 Zustand Store 实例。
 *
 * 仅管理本地持久化的厂商清单内存态与写入，不发起任何网络请求。
 *
 * @returns Zustand hook；对话框组件调用后可读已配置厂商清单。
 */
export const useProviderConfigStore = create<ProviderConfigState & ProviderConfigActions>((set) => ({
  configuredProviders: loadConfiguredProviders(),

  setConfiguredProviders: (providers) => {
    set({ configuredProviders: providers });
    persistConfiguredProviders(providers);
  },

  upsertConfiguredProvider: (provider) => {
    set((state) => {
      const next = state.configuredProviders.filter((item) => item.provider_id !== provider.provider_id);
      next.unshift(provider);
      persistConfiguredProviders(next);
      return { configuredProviders: next };
    });
  },

  removeConfiguredProvider: (providerId) => {
    set((state) => {
      const next = state.configuredProviders.filter((item) => item.provider_id !== providerId);
      persistConfiguredProviders(next);
      return { configuredProviders: next };
    });
  },
}));
