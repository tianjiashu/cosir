/**
 * 模型厂商域共享契约（Provider / Model）。
 *
 * 本文件承载手写部分（前端聚合/语义类型与 API 路径常量）；
 * 派生于后端 schema 的请求体类型（ProviderCreateRequest / ProviderUpdateRequest）
 * 由 `scripts/generate_api_ts.py` 生成于 `modelRequests.ts`，
 * 经下方 re-export 透出，避免手写漂移。字段命名沿用后端 snake_case。
 *
 * @module shared/model
 */

/**
 * 模型厂商域 API 路径常量（无 /api 前缀，与 API_PATHS 约定一致）。
 *
 * 仅保留后端真实存在的端点，避免后续开发者误用不存在的路由：
 * - 保留 PROVIDERS / PROVIDER_DETAIL / PROVIDER_TEST / MODELS（后端存在）；
 * - 已删除 PROVIDER_DISCOVER / PROVIDER_MODELS / MODEL_DETAIL：后端无对应端点
 *   （discover/import/PUT|DELETE /models/{id} 均不存在），保留会诱惑误用产生 404。
 */
export const MODEL_PROVIDER_PATHS = {
  PROVIDERS: "/providers",
  PROVIDER_DETAIL: (providerId: number | string) => `/providers/${providerId}`,
  PROVIDER_TEST: (providerId: number | string) => `/providers/${providerId}/test`,
  MODELS: "/models",
} as const;

/**
 * 允许的厂商类型枚举（决定默认 base_url，与后端 PROVIDER_TYPES 对齐）。
 *
 * 15 类与后端 ``app/models/provider_capability.py`` 的 ``PROVIDER_CAPABILITIES``
 * 注册表键一一对应；新增厂商 = 改后端注册表一行 + 这里加一个联合分支 +
 * ``shared-model-contract.test.ts`` 契约断言。
 */
export type ProviderType =
  | "deepseek"
  | "openai-compatible"
  | "anthropic"
  | "gemini"
  | "azure"
  | "dashscope"
  | "moonshot"
  | "zai"
  | "volcengine"
  | "tencent"
  | "minimax"
  | "ollama"
  | "qianfan"
  | "xfyun"
  | "custom";

/**
 * 厂商配置记录（POST /providers 回参与前端本地持久化参考）。
 *
 * 由后端 ``ProviderResponse``（``modelResponses.ts``）派生，仅保留前端语义特化：
 * ``type`` 用字面量联合 ``ProviderType``（后端是宽松 ``string``）。其余字段（含
 * ``provider_id`` 已对齐后端 int）与后端响应一致，不再手写死字段，避免漂移。
 */
export type ProviderRecord = Omit<ProviderResponse, "type"> & {
  /** 厂商类型（前端字面量联合，后端为宽松 string）。 */
  type: ProviderType;
};

/** 派生自后端 schema 的请求体类型，由 `modelRequests.ts` 生成（re-export 透出）。 */
export type {
  ProviderCreateRequest,
  ProviderUpdateRequest,
} from "./modelRequests";

/** 派生自后端 schema 的响应类型，由 `modelResponses.ts` 生成（供前端消费类型派生）。 */
import type { ModelEntryResponse, ProviderResponse } from "./modelResponses";

/**
 * 连通性测试结果（POST /providers/{id}/test 响应，设计文档 §三 用户视角三件套）。
 */
export interface ProviderConnectionTestResult {
  /** 被测试的厂商标识（后端 int 主键）。 */
  provider_id: number;
  /** 测试是否成功（成功时 error_code / error_message 均为 null）。 */
  success: boolean;
  /** 测试耗时（毫秒），供 UI 展示「响应速度」。 */
  elapsed_ms: number;
  /** 失败时的稳定错误码（与后端 ``ErrorKind`` 枚举对齐；骨架阶段返回 ``unknown_error``）。 */
  error_code: string | null;
  /** 失败时面向用户的可读中文消息（前端直接展示）。 */
  error_message: string | null;
}

/**
 * 模型推理强度能力（对齐后端 ``ModelCapability.reasoning_effort``）。
 *
 * 后端 ``/models`` 端点（``models_api.py``）通过 ``dataclasses.asdict(model_capability.reasoning_effort)``
 * 填充该字段；``ReasoningEffortCapability`` 默认 ``supported=False``、``effort_map={}``，**永不返回 null**，
 * 即当前线上 ``/models`` 实际始终返回含 ``supported``/``effort_map`` 的 dict（非 null）。
 * 前端保留 ``ReasoningEffortInfo | null`` 的可空性仅为**防御性容错**：兼容「旧缓存或后端未返回该字段」
 * 的历史数据，避免运行时判空崩溃；新契约下消费方仍应以 ``?.supported`` 判空读取
 * （``supported=false`` 表示模型不支持推理强度档位选择，前端不渲染控件；
 * ``supported=true`` 时 ``effort_map`` 给出前端可渲染的档位键集合，内部档位名 → 厂商原始档位映射）。
 */
export interface ReasoningEffortInfo {
  /** 模型是否支持推理强度档位选择（后端实际恒非空，前端对旧缓存/缺字段以 null 容错）。 */
  supported: boolean;
  /** 内部档位名到厂商原始档位的映射（前端档位控件使用键，如低/高/最大）。 */
  effort_map: Record<string, string>;
}

/**
 * 模型条目记录（由后端 ``ModelEntryResponse`` 派生，``modelResponses.ts`` 为唯一事实来源）。
 *
 * 契约收敛说明（重要，避免维护者误引不存在的字段）：
 * ``GET /models`` 是前端模型数据**唯一且充分的真实数据源**，其实际投影字段见
 * 后端 ``ModelEntryResponse``（``modelResponses.ts``）。以下字段后端 ``GET /models``
 * **不返回**，已从派生基类剔除，前端不得消费：``model_id / display_name /
 * max_context_window / created_at / updated_at``。
 * 注：后端 ``ModelEntryService`` 模型表确实存在 ``max_context_window`` 列，但端点未投影，
 * 故前端不渲染窗口徽标（诚实省略，不显示伪造 0），非漏实现。
 *
 * 前端语义特化（保留，非漂移）：
 * - ``reasoning_effort``：后端为宽松 ``Record<string, unknown>``，前端包装为
 *   ``ReasoningEffortInfo | null``（带 ``supported`` / ``effort_map`` 语义），``null``
 *   仅防御旧缓存/缺字段。消费方以 ``?.supported`` 判空。
 * - ``supports_image`` / ``supports_video``：后端为必填 ``boolean``，前端改为可选（旧缓存
 *   或缺失该字段时视为未声明，前端图片拦截以「明确为 false 才拦截」为准）。
 */
/**
 * 按模型身份二元组在模型列表中定位条目（跨厂商重名安全的唯一匹配方式）。
 *
 * 后端以 ``(provider_id, model_name)`` 二元组标识一个可用模型，同一 model_name 可能
 * 分属不同厂商（如 openai/gpt-4o 与 azure/gpt-4o）。因此凡「判断某模型是否仍是用户
 * 选中的那个」的场景都必须同时比对两个字段，仅比对 model_name 会把跨厂商同名模型
 * 误判为命中。
 *
 * 本函数用于「从列表中找出选中条目」，供发送前校验、档位校准、折叠态标签等处复用；
 * 若已有候选条目、只需判等，请改用 ``isModelSelection``（按字段判等，不依赖元素引用
 * 身份）。两者共同构成二元组判定的唯一收口，避免比对逻辑散落各处。
 *
 * @param models - 待搜索的模型列表（通常来自 GET /models）。
 * @param selection - 目标模型身份二元组；null（未选择）时直接返回 undefined。
 * @returns 命中返回该模型条目；未命中或未选择返回 undefined。
 */
export function findModelBySelection(
  models: ModelEntryRecord[],
  selection: { provider_id: number; model_name: string } | null,
): ModelEntryRecord | undefined {
  if (selection === null) {
    return undefined;
  }
  return models.find(
    (model) =>
      model.model_name === selection.model_name && model.provider_id === selection.provider_id,
  );
}

/**
 * 判定某个模型条目是否正是用户当前选中的那个（二元组字段判等）。
 *
 * 与 ``findModelBySelection`` 语义等价，但用于「已有候选条目、只需判等」的场景
 * （如渲染列表时逐行判断是否高亮）。直接比对字段而非比对数组元素引用身份——
 * 引用相等要求候选条目与搜索源数组是同一批对象，一旦列表被 map 重建
 * （如将来加缓存包装或派生字段）就会静默失效，故此处以字段判等为准。
 *
 * @param model - 待判定的模型条目。
 * @param selection - 当前选中的模型身份二元组；null（未选择）时恒返回 false。
 * @returns 该条目即为选中模型时返回 true；否则返回 false。
 */
export function isModelSelection(
  model: ModelEntryRecord,
  selection: { provider_id: number; model_name: string } | null,
): boolean {
  if (selection === null) {
    return false;
  }
  return model.model_name === selection.model_name && model.provider_id === selection.provider_id;
}

export type ModelEntryRecord = Omit<
  ModelEntryResponse,
  "reasoning_effort" | "supports_image" | "supports_video"
> & {
  /**
   * 模型推理强度能力（来源：后端 ``ModelEntryResponse.reasoning_effort``）。
   * 类型为 ``ReasoningEffortInfo | null``：后端当前实际始终返回含 ``supported``/``effort_map``
   * 的非空 dict，``null`` 仅为前端对「旧缓存或后端未返回该字段」的防御性容错。
   * 消费方必须以 ``?.supported`` 判空后再读。
   */
  reasoning_effort: ReasoningEffortInfo | null;
  /** 模型是否支持图片/视觉输入（可选，缺字段视为未声明）。 */
  supports_image?: boolean;
  /** 模型是否支持视频输入（可选，缺字段视为未声明）。 */
  supports_video?: boolean;
};
