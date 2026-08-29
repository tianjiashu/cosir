/**
 * 任务容器状态管理（Zustand）。
 *
 * 管理：
 * - 按 task_id 索引的任务实体缓存（tasksById）
 * - 按 workspace_id 分组的任务列表缓存（tasksByWorkspaceId）
 * - 已加载工作区集合（loadedWorkspaceIds）：加载态与数据态正交，不靠数组是否存在兼职
 * - 当前活跃任务
 * - 任务状态变更动作
 *
 * 加载态契约：``loadedWorkspaceIds`` 含某 workspaceId 表示该 workspace 的任务列表已
 * 经从后端拉取（即便为空也是 ``[]``）；不含表示尚未加载（惰性填充）。侧栏据此判定是否
 * 需补拉，新增任务在未加载分组时不得伪造 ``[task]`` 以免永久跳过真实拉取。
 *
 * 派生 UI 状态（如运行状态标签）由 store 数据计算，不单独冗余存储。
 * 首屏活跃任务恢复由 App 层（resumeAttempted 分支）单点负责，本 store 不内嵌恢复编排，
 * 以确保「展开 workspace 永不意外切走中央对话」这一不变量。
 *
 * @module stores/taskStore
 */

import { create } from "zustand";
import type { TaskRecord } from "@shared/task";
import type { TaskStatus } from "@shared/task";
import type { ModelEntryRecord } from "@shared/model";
import { findModelBySelection } from "@shared/model";
import { listModels } from "@/services/api";
import { useEventStore } from "@/stores/eventStore";
import { useContextUsageStore } from "@/stores/contextUsageStore";
import { logWarn } from "@/lib/logger";

/**
 * 用户显式选定的模型身份（后端模型身份的完整表达）。
 *
 * 后端以 ``(provider_id, model_name)`` 二元组唯一标识一个可用模型：``provider_id``
 * 指向 ``providers.id``，``model_name`` 为 litellm 路由名。创建轮次时两者必须成对
 * 提交（``turn_service.create_turn`` 对「有 model_name 无 provider_id」直接抛
 * ``ValueError``，API 层映射为 400），故前端选择态必须完整持有二元组，不能只存
 * model_name 再在发送时反查——反查在模型列表未加载或模型已删除时会失败。
 */
export interface SelectedModel {
  /** 模型归属厂商标识（后端 providers.id）。 */
  provider_id: number;
  /** 模型路由名（litellm 路由名，与 provider_id 配对使用）。 */
  model_name: string;
}

/**
 * 上次活跃任务 ID 的本地持久化键。
 *
 * 用于进入应用时自动恢复上次正在查看的任务，避免每次都需要手动点击侧栏任务
 * 才能加载中央会话区。仅持久化真实任务 ID（"temp-" 前缀的临时任务不持久化）。
 */
const ACTIVE_TASK_STORAGE_KEY = "coding-agent.activeTaskId";

/**
 * 当前选中模型的本地持久化键。
 *
 * 用于进入应用时自动恢复上次显式选定的模型。落盘值是 ``SelectedModel`` 的 JSON
 * （含 ``provider_id`` 与 ``model_name`` 两个字段），而非裸模型名字符串——后端以
 * ``(provider_id, model_name)`` 二元组标识一个模型，单字符串会丢失厂商归属，
 * 导致创建轮次时无法配对 ``provider_id`` 而被后端 400 拒绝。
 * 模型为 null（未选择）时清除持久化值，确保「未选择」语义在重启后保持一致
 * （设计 §9.1）。
 */
const SELECTED_MODEL_STORAGE_KEY = "coding-agent.selectedModel";

/**
 * 当前选中推理强度档位的本地持久化键。
 *
 * 用于进入应用时自动恢复上次显式选定的推理强度档位，与选中模型名配套。
 * 仅持久化非空档位名；档位为 null（未指定，同后端 None=max/不指定）时
 * 清除持久化值，确保「未指定」语义在重启后保持一致。
 */
const SELECTED_REASONING_EFFORT_STORAGE_KEY = "coding-agent.selectedReasoningEffort";

/**
 * 按任务维度持久化输入草稿的本地存储键。
 *
 * 草稿以 ``{ [taskId]: string }`` 的 JSON 形式落盘，用于跨 task 切换/刷新后恢复
 * 未发送的输入内容（借鉴 deepseek-harness 的 per-session 草稿镜像，见
 * ``docs/输入组件优化.md`` §4.5/§8.6）。空草稿不占用键（写入空字符串即删除该任务条目），
 * 避免存储随任务数无限膨胀。
 */
const INPUT_DRAFTS_STORAGE_KEY = "coding-agent.inputDrafts";

/**
 * 判断 localStorage 原始字符串是否为可持久化的真实任务 ID（排除 "temp-" 前缀的临时任务）。
 *
 * 持久化层只与 localStorage 的 string 交互；真实任务 ID 在内存中是 number，但落盘时被
 * `String()` 转 string、读取时由 `loadPersistedActiveTaskId` 经 `Number()` 还原。本函数
 * 仅在「读取后的 string 是否代表真实任务」这一粒度判定，不涉及 number 转换。
 *
 * @param rawTaskId - 来自 localStorage 的原始字符串（可能为 null）。
 * @returns 为可持久化的真实任务 ID 字符串时返回 true。
 */
function isPersistableTaskId(rawTaskId: string | null): boolean {
  return !!rawTaskId && !rawTaskId.startsWith("temp-");
}

/**
 * 读取本地持久化的上次活跃任务 ID，还原为内存 number 语义。
 *
 * localStorage 只能存 string，故真实任务 ID 落盘时为十进制字符串；此处经 `Number()`
 * 还原为后端 int 主键（number）。localStorage 不可用（如隐私模式）或值非可持久化 ID
 * 时记录 WARN 日志后仍返回 null，不影响主流程。
 *
 * @returns 持久化的任务 ID（number）；无有效值时返回 null。
 */
export function loadPersistedActiveTaskId(): number | null {
  try {
    const raw = localStorage.getItem(ACTIVE_TASK_STORAGE_KEY);
    if (!raw || !isPersistableTaskId(raw)) {
      return null;
    }
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : null;
  } catch (err) {
    logWarn("读取持久化活跃任务失败，降级为无恢复态", { module: "taskStore", error: err instanceof Error ? err.message : String(err) });
    return null;
  }
}

/**
 * 持久化活跃任务 ID 到本地存储。
 *
 * 仅持久化真实任务 ID（number）；临时任务或 null 时清除持久化值。真实 number ID 经
 * `String()` 转为十进制字符串落盘（localStorage 只接受 string）。localStorage 不可用时
 * 记录 WARN 日志，不抛错、不影响内存状态与交互。
 *
 * @param taskId - 待持久化的任务 ID（number，后端 int 主键）；临时任务或 null 时清除持久化值。
 */
function persistActiveTaskId(taskId: number | null): void {
  try {
    if (taskId !== null && taskId > 0 && !Number.isNaN(taskId)) {
      localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, String(taskId));
    } else {
      localStorage.removeItem(ACTIVE_TASK_STORAGE_KEY);
    }
  } catch (err) {
    logWarn("持久化活跃任务失败", { module: "taskStore", task_id: taskId, error: err instanceof Error ? err.message : String(err) });
  }
}

/**
 * 读取本地持久化的上次选中模型。
 *
 * 落盘值是 ``SelectedModel`` 的 JSON（含 ``provider_id`` 与 ``model_name``）；解析失败、
 * 字段缺失或类型不符时一律降级为 null（即未选择），不抛出、不阻塞启动。
 * localStorage 不可用（如隐私模式）时同样降级为 null。
 *
 * @returns 持久化的选中模型二元组；无有效值时返回 null（未选择）。
 */
export function loadPersistedSelectedModel(): SelectedModel | null {
  try {
    const raw = localStorage.getItem(SELECTED_MODEL_STORAGE_KEY);
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw) as unknown;
    // 二元组缺一不可：仅校验出完整二元组才视为有效选择，其余（旧格式裸字符串、
    // 缺字段、类型错误）统一降级为未选择——绿地项目不做旧格式迁移。
    if (isSelectedModel(parsed)) {
      return { provider_id: parsed.provider_id, model_name: parsed.model_name };
    }
    logWarn("持久化选中模型格式非法，降级为未选择", { module: "taskStore" });
    return null;
  } catch (err) {
    logWarn("读取持久化选中模型失败，降级为未选择", { module: "taskStore", error: err instanceof Error ? err.message : String(err) });
    return null;
  }
}

/** 判定未知解析结果是否为合法的 ``SelectedModel`` 二元组。 */
function isSelectedModel(value: unknown): value is SelectedModel {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate["provider_id"] === "number" &&
    Number.isFinite(candidate["provider_id"]) &&
    typeof candidate["model_name"] === "string" &&
    candidate["model_name"].length > 0
  );
}

/**
 * 持久化选中模型到本地存储。
 *
 * 显式选择模型时以 JSON 写入完整二元组；模型为 null（未选择）时清除持久化值。
 * localStorage 不可用时记录 WARN 日志，不抛错、不影响内存状态与交互。这是
 * 「凡改 selectedModel 必同步 localStorage」这一不变量的唯一出口，避免各分支用
 * set 直写绕过持久化导致内存态与持久化态撕裂。
 *
 * @param model - 待持久化的选中模型二元组；null（未选择）时清除持久化值。
 */
function persistSelectedModel(model: SelectedModel | null): void {
  try {
    if (model) {
      localStorage.setItem(SELECTED_MODEL_STORAGE_KEY, JSON.stringify(model));
    } else {
      localStorage.removeItem(SELECTED_MODEL_STORAGE_KEY);
    }
  } catch (err) {
    logWarn("持久化选中模型失败", { module: "taskStore", model_name: model?.model_name ?? null, error: err instanceof Error ? err.message : String(err) });
  }
}

/**
 * 读取本地持久化的上次选中推理强度档位。
 *
 * localStorage 不可用（如隐私模式）时记录 WARN 日志后仍返回 null（即未指定，
 * 同后端 None=max/不指定），不影响主流程与发送链路。
 *
 * @returns 持久化的档位名；无有效值时返回 null（未指定）。
 */
export function loadPersistedSelectedReasoningEffort(): string | null {
  try {
    const value = localStorage.getItem(SELECTED_REASONING_EFFORT_STORAGE_KEY);
    return value ? value : null;
  } catch (err) {
    logWarn("读取持久化选中推理强度失败，降级为未指定", { module: "taskStore", error: err instanceof Error ? err.message : String(err) });
    return null;
  }
}

/**
 * 持久化选中推理强度档位到本地存储。
 *
 * 显式选择档位时写入；档位为 null（未指定）时清除持久化值。localStorage 不可用时
 * 记录 WARN 日志，不抛错、不影响内存状态与交互。这是「凡改 selectedReasoningEffort
 * 必同步 localStorage」这一不变量的唯一出口，避免各分支用 set 直写绕过持久化
 * 导致内存态与持久化态撕裂（与 persistSelectedModel 同范式）。
 *
 * @param effort - 待持久化的档位名；null（未指定）时清除持久化值。
 */
function persistSelectedReasoningEffort(effort: string | null): void {
  try {
    if (effort) {
      localStorage.setItem(SELECTED_REASONING_EFFORT_STORAGE_KEY, effort);
    } else {
      localStorage.removeItem(SELECTED_REASONING_EFFORT_STORAGE_KEY);
    }
  } catch (err) {
    logWarn("持久化选中推理强度失败", { module: "taskStore", effort, error: err instanceof Error ? err.message : String(err) });
  }
}

/**
 * 读取本地持久化的全部任务输入草稿。
 *
 * localStorage 不可用或 JSON 损坏时记录 WARN 日志后返回空映射，不影响主流程。
 * 落盘的键是 task_id 的十进制字符串，此处经 `Number()` 还原为 number 键
 * （与内存 `drafts: Record<number, string>` 对齐）。
 *
 * @returns 按任务 ID（number）索引的草稿映射；无有效值时返回空对象。
 */
function loadPersistedInputDrafts(): Record<number, string> {
  try {
    const raw = localStorage.getItem(INPUT_DRAFTS_STORAGE_KEY);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw) as unknown;
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      // 过滤掉非字符串值与非数字键，避免脏数据污染草稿；键还原为 number。
      // 草稿键只可能是 number（真实任务 ID 为正、乐观占位为负），不存在 "temp-" 字符串键
      // （该前缀仅用于 SSE 连接键，从不落 drafts）。
      const drafts: Record<number, string> = {};
      for (const [rawTaskId, value] of Object.entries(parsed as Record<string, unknown>)) {
        if (typeof value === "string" && value.length > 0) {
          const taskId = Number(rawTaskId);
          if (Number.isFinite(taskId)) {
            drafts[taskId] = value;
          }
        }
      }
      return drafts;
    }
    return {};
  } catch (err) {
    logWarn("读取持久化输入草稿失败，降级为空", { module: "taskStore", error: err instanceof Error ? err.message : String(err) });
    return {};
  }
}

/**
 * 草稿落盘节流间隔（毫秒）。
 *
 * 高频输入时每次敲击都全量读写 localStorage 会造成无谓 IO；经 trailing debounce
 * 合并为「停顿后一次性落盘」。清除类写入（空草稿）不节流——它们低频且语义关键
 * （发送成功清空/任务删除），延迟落盘会在刷新后复活已发送内容。
 */
const INPUT_DRAFT_PERSIST_DEBOUNCE_MS = 300;

/** 待落盘的草稿写入（taskId → 定时器），trailing debounce 用。 */
const pendingDraftPersistTimers = new Map<number, ReturnType<typeof setTimeout>>();

/**
 * 立即把指定任务草稿写入 localStorage（同步落盘，无节流）。
 *
 * @param taskId - 目标任务 ID（number，后端 int 主键）。
 * @param draft - 草稿内容；空字符串表示删除该任务条目（保持存储精简）。
 */
function writeInputDraftToStorage(taskId: number, draft: string): void {
  try {
    const drafts = loadPersistedInputDrafts();
    if (draft.length > 0) {
      drafts[taskId] = draft;
    } else {
      delete drafts[taskId];
    }
    localStorage.setItem(INPUT_DRAFTS_STORAGE_KEY, JSON.stringify(drafts));
  } catch (err) {
    logWarn("持久化输入草稿失败", { module: "taskStore", task_id: taskId, error: err instanceof Error ? err.message : String(err) });
  }
}

/**
 * 持久化指定任务的输入草稿到本地存储（唯一出口，含节流）。
 *
 * 语义：
 * - 非空草稿：trailing debounce（300ms）后落盘，合并高频输入。
 * - 空草稿（清除）：立即落盘并取消该任务待执行的去抖写入，防止清除被后续
 *   trailing 写入覆盖复活。
 *
 * localStorage 不可用时记录 WARN 日志，不抛错、不影响内存状态与交互。这是
 * 「凡改某任务草稿必同步 localStorage」这一不变量的唯一出口，避免组件层各自
 * 直写绕过持久化。
 *
 * @param taskId - 目标任务 ID（number，后端 int 主键）。
 * @param draft - 待持久化的草稿内容；空字符串表示清除该任务草稿。
 */
function persistInputDraft(taskId: number, draft: string): void {
  const existing = pendingDraftPersistTimers.get(taskId);
  if (existing !== undefined) {
    clearTimeout(existing);
    pendingDraftPersistTimers.delete(taskId);
  }
  if (draft.length === 0) {
    // 清除语义关键且低频：立即落盘。
    writeInputDraftToStorage(taskId, draft);
    return;
  }
  const timer = setTimeout(() => {
    pendingDraftPersistTimers.delete(taskId);
    writeInputDraftToStorage(taskId, draft);
  }, INPUT_DRAFT_PERSIST_DEBOUNCE_MS);
  pendingDraftPersistTimers.set(taskId, timer);
}

/**
 * 草稿迁移的落盘指令：转正时需「写入真实 ID」并「清除占位键」两笔。
 *
 * 由 ``migrateDraftEntry`` 计算、``applyDraftMigration`` 执行，使内存迁移与落盘
 * 同源于一次计算，避免调用方各自推导两步写入而产生时序竞态。
 */
interface DraftMigrationPersist {
  /** 待写入真实任务 ID 的草稿内容；无草稿时为 null（无需写入）。 */
  draftForRealTask: string | null;
  /** 真实任务 ID（草稿的目标键）。 */
  realTaskId: number;
  /** 待清除的乐观占位任务 ID。 */
  placeholderTaskId: number;
}

/**
 * 计算「乐观占位任务转正」时的草稿迁移结果（纯函数，无副作用）。
 *
 * 迁移是原子语义：草稿要么整体迁到真实 ID，要么保持原状；不存在「写一半」的中间态。
 * 调用方（``replaceTask``）把返回的 ``drafts`` 写入内存、把 ``persist`` 交给
 * ``applyDraftMigration`` 落盘，两侧同源，杜绝重复推导。
 *
 * @param drafts - 当前草稿映射（不会被修改，返回新对象）。
 * @param oldTaskId - 乐观占位任务 ID（负 number）。
 * @param realTaskId - 后端返回的真实任务 ID（正 number）。
 * @returns ``drafts`` 为迁移后的新草稿映射；占位键上无草稿时为 undefined（无需变更）。
 *   ``persist`` 为配套的落盘指令。
 */
function migrateDraftEntry(
  drafts: Record<number, string>,
  oldTaskId: number,
  realTaskId: number,
): { drafts?: Record<number, string>; persist: DraftMigrationPersist } {
  const migrated = drafts[oldTaskId];
  if (migrated === undefined) {
    // 占位键上无草稿：仅清占位键（幂等），不写入真实 ID。
    return {
      persist: { draftForRealTask: null, realTaskId, placeholderTaskId: oldTaskId },
    };
  }
  const next = { ...drafts };
  next[realTaskId] = migrated;
  delete next[oldTaskId];
  return {
    drafts: next,
    persist: { draftForRealTask: migrated, realTaskId, placeholderTaskId: oldTaskId },
  };
}

/**
 * 执行草稿迁移的落盘（唯一出口）。
 *
 * @param migration - 由 ``migrateDraftEntry`` 计算出的落盘指令；null 表示无需落盘。
 * @returns 无。
 *
 * @sideeffect
 * - 有草稿时写入真实任务 ID 的草稿；随后清除占位键（清除为立即写入，不节流）。
 * - 持久化失败仅记 WARN，不影响已完成的内存态迁移。
 */
function applyDraftMigration(migration: DraftMigrationPersist | null): void {
  if (migration === null) {
    return;
  }
  if (migration.draftForRealTask !== null) {
    persistInputDraft(migration.realTaskId, migration.draftForRealTask);
  }
  persistInputDraft(migration.placeholderTaskId, "");
}

/**
 * 计算「选中模型变更后」推理强度档位应保留还是清空（纯函数，安全默认）。
 *
 * 档位归属特定模型的特定能力，切换/清空模型后残留档位即无意义。本函数是这条
 * 业务规则的唯一收口，供 ``setSelectedModel``（切换时）与 ``refreshAvailableModels``
 * （列表到齐后校准）两处复用，避免同一规则出现平行实现。
 *
 * 安全默认：凡不能确认「新模型支持强度」的情形（未选择模型、模型缺失、模型未
 * 声明支持）一律清空——null 对后端语义为「不指定/max」，无害；而残留一个属于
 * 旧模型的档位则可能把无效值透传给后端。
 *
 * @param model - 变更后的模型条目；null 表示未选择模型。
 * @param currentEffort - 变更前的档位值。
 * @returns 应保留返回原档位；应清空返回 null。
 */
function resolveEffortAfterModelChange(
  model: ModelEntryRecord | undefined | null,
  currentEffort: string | null,
): string | null {
  if (currentEffort === null) {
    // 本就没有档位，无需清理。
    return null;
  }
  if (model === null || model === undefined) {
    return null;
  }
  return model.reasoning_effort?.supported === true ? currentEffort : null;
}

/**
 * 统一设置活跃任务并同步持久化。
 *
 * 这是「凡改 activeTaskId 必同步 localStorage」这一不变量的唯一出口，避免
 * 各分支用 ``set`` 直写绕过持久化导致内存态与持久化态撕裂。任务记录经
 * ``get().tasksById`` 查找，未加载分组中查不到不影响选中与主对话区渲染。
 *
 * @param set - Zustand 的 set 函数。
 * @param taskId - 目标活跃任务 ID（number，后端 int 主键；可为 null 表示清空）。
 * @param turnId - 可选的目标活跃轮次 ID（number，后端 int 主键）；缺省时置为 null，
 *   真实轮次 ID 由调用方（useTask）经 turnsByTask 显式设置。
 */
function applyActiveTask(
  set: (partial: Partial<TaskState>) => void,
  taskId: number | null,
  turnId?: number | null,
): void {
  set({
    activeTaskId: taskId,
    activeTurnId: turnId ?? null,
  });
  persistActiveTaskId(taskId);
}

/** 任务 Store 的状态接口。 */
interface TaskState {
  /**
   * 按 task_id（后端 int 主键）索引的任务实体缓存；主对话区与顶部状态读取该事实源，
   * 不依赖 workspace 列表是否已加载。键类型为 number，与 TaskRecord.task_id 一致。
   */
  tasksById: Record<number, TaskRecord>;
  /** 按 workspace_id（后端 int 主键）分组缓存的任务列表，每个 workspace 独立维护自身任务。 */
  tasksByWorkspaceId: Record<number, TaskRecord[]>;
  /** 已向后端拉取过任务列表的工作区 ID 集合（后端 int 主键）；不含表示尚未加载（惰性填充）。 */
  loadedWorkspaceIds: Set<number>;
  /** 当前正在查看/交互的任务 ID（number，后端 int 主键；不一定在运行中）。 */
  activeTaskId: number | null;
  /** 当前活跃轮次 ID（number，后端 int 主键）。 */
  activeTurnId: number | null;
  /**
   * 当前选中的模型身份（provider_id + model_name 二元组）；null 表示未选择
   * （需显式选择后发送，设计 §9.1）。持完整二元组而非裸模型名，才能与后端
   * 创建轮次的配对契约对齐，详见 ``SelectedModel`` 类型注释。
   */
  selectedModel: SelectedModel | null;
  /** 当前选中的推理强度档位；null 表示未指定（同后端 None=max/不指定）。 */
  selectedReasoningEffort: string | null;
  /** 后端可用模型列表（来自 /models 聚合端点），供 ModelSelector 下拉渲染。 */
  availableModels: ModelEntryRecord[];
  /** 可用模型列表是否已成功加载（避免空数组与未加载态混淆）。 */
  modelsLoaded: boolean;
  /**
   * 按任务 ID（number，后端 int 主键）索引的输入草稿缓存（跨 task 切换/刷新后恢复未发送内容）。
   *
   * 键为 number 任务 ID。乐观新建任务期间以负 number 占位键（``-1 - seq``）暂存草稿，
   * 后端返回真实 task 后由 ``replaceTask`` 迁移到真实任务 ID。空草稿不占用键。
   */
  drafts: Record<number, string>;
}

/** 任务 Store 的动作接口。 */
interface TaskActions {
  /** 写入指定 workspace 的任务列表并标记该 workspace 已加载（惰性加载/刷新用）。 */
  setWorkspaceTasks: (workspaceId: number, tasks: TaskRecord[]) => void;
  /** 判断指定 workspace 是否已从后端加载过任务列表。 */
  isWorkspaceLoaded: (workspaceId: number) => boolean;
  /** 添加一个新任务到所属 workspace 分组。 */
  addTask: (task: TaskRecord) => void;
  /**
   * 用正式任务替换临时任务。
   *
   * 前端乐观创建任务时先用负 number 占位 task_id（`-1 - seq`）写入 `tasksById` / 分组 /
   * 草稿，该占位与真实 task 同为 number 类型，与 `TaskRecord.task_id` 一致；待后端返回
   * 真实 task（number 主键）后调用本方法把临时记录整条替换为真实记录。
   *
   * 临时 id 的 string 维度（"temp-<uuid>"）仅用于 SSE 连接键，不写入 store 的 number 字段，
   * 故本方法定位临时记录用 number 占位 `oldTaskId` 直接比对 `tasksById` 的 number 键，
   * 不再经由 Object.entries 的 string 中间态，避免 number↔string 边界失配。
   *
   * @param oldTaskId - 被替换的临时任务占位（number，乐观创建期间由前端生成的负 number，
   *   如 `-1 - seq`）；用于在 `tasksById` / 分组 / 草稿中定位并移除临时记录。
   * @param task - 后端返回的真实任务（task_id 为 number 主键）。
   */
  replaceTask: (oldTaskId: number, task: TaskRecord) => void;
  /**
   * 删除一个任务（跨分组清理）。
   * 内部遍历 ``tasksByWorkspaceId`` 时直接以 number 键（workspace_id）迭代并回写
   * ``Record<number>``，保持键类型为后端 int 主键，不经由 Object.entries 的 string 中间态。
   *
   * @param taskId - 待删除的任务 ID（number，后端 int 主键）。
   */
  removeTask: (taskId: number) => void;
  /**
   * 更新任务状态（根据 task_id 跨分组匹配并替换）。
   * 内部遍历 ``tasksByWorkspaceId`` 时直接以 number 键（workspace_id）迭代并回写
   * ``Record<number>``，保持键类型为后端 int 主键，不经由 Object.entries 的 string 中间态。
   *
   * @param taskId - 待更新的任务 ID（number，后端 int 主键）。
   * @param updates - 待合并到该任务的局部字段。
   */
  updateTask: (taskId: number, updates: Partial<TaskRecord>) => void;
  /**
   * 设置当前活跃任务。
   *
   * @param taskId - 目标活跃任务 ID（number，后端 int 主键；可为 null 表示清空）。
   * @param turnId - 可选的目标活跃轮次 ID（number，后端 int 主键）；缺省时置为 null。
   */
  setActiveTask: (taskId: number | null, turnId?: number | null) => void;
  /**
   * 设置当前活跃轮次。
   *
   * @param turnId - 目标活跃轮次 ID（number，后端 int 主键；可为 null 表示清空）。
   */
  setActiveTurn: (turnId: number | null) => void;
  /**
   * 设置当前选中的模型身份。
   *
   * 入参是完整的模型条目（``ModelEntryRecord``）而非裸模型名：二元组由本方法从条目
   * 上统一抽取，从结构上杜绝「只设了 model_name 却漏 provider_id」导致后端 400。
   *
   * @param model - 选中的模型条目（来自 ``availableModels``）；null 表示清空选择（同时清档位）。
   */
  setSelectedModel: (model: ModelEntryRecord | null) => void;
  /**
   * 设置当前选中的推理强度档位；null 表示未指定（同后端 None=max/不指定）。
   * 调用方应先确认目标模型支持强度（reasoning_effort?.supported === true）再传入档位。
   */
  setSelectedReasoningEffort: (effort: string | null) => void;
  /**
   * 从后端拉取可用模型列表并写入 store（幂等，可重复调用刷新）。
   * 失败仅记录 WARN 日志，不抛出、不影响主流程。
   */
  refreshAvailableModels: () => Promise<boolean>;
  /** 清空所有任务数据。 */
  clearTasks: () => void;
  /** 清理指定 workspace 的分组（删除工作区时级联）。 */
  /**
   * 取某工作区下全部已知任务 id（合并两个来源并去重）。
   *
   * 不能只读 ``tasksByWorkspaceId``：该分组是**懒加载**的，工作区未被展开过时为空，
   * 会漏掉「已打开过、正在流式但分组未加载」的任务。``tasksById`` 才是已打开任务的
   * 权威全集。任何按工作区批量操作（清理缓存、断流）都必须用本方法取 id，否则会漏。
   */
  getWorkspaceTaskIds: (workspaceId: number) => number[];
  clearWorkspaceTasks: (workspaceId: number) => void;
  /**
   * 根据任务 ID 获取任务记录。
   * @param taskId - 查找的目标任务 ID（number，后端 int 主键）。
   * @returns 匹配的任务记录，未找到返回 undefined。
   */
  getTaskById: (taskId: number) => TaskRecord | undefined;
  /**
   * 写入指定任务的输入草稿并同步持久化（单一出口）。
   * 空草稿自动清除该任务条目；持久化失败仅记录 WARN，不影响内存态。
   *
   * @param taskId - 目标任务 ID（number，后端 int 主键）。
   * @param draft - 待持久化的草稿内容；空字符串表示清除该任务草稿。
   */
  setInputDraft: (taskId: number, draft: string) => void;
}

/**
 * 任务 Zustand Store 实例。
 *
 * 使用 zustand create API，支持 React 组件外直接调用
 * （如 service 层更新后同步 store）。
 */
export const useTaskStore = create<TaskState & TaskActions>((set, get) => ({
  // --- 初始状态 ---
  tasksById: {},
  tasksByWorkspaceId: {},
  loadedWorkspaceIds: new Set(),
  // 进入应用时优先恢复上次活跃任务（持久化于 localStorage）；无记录时为 null。
  activeTaskId: loadPersistedActiveTaskId(),
  activeTurnId: null,
  // 进入应用时优先恢复上次选中模型（持久化于 localStorage）；无记录时为 null（未选择）。
  selectedModel: loadPersistedSelectedModel(),
  // 进入应用时优先恢复上次选中推理强度档位（持久化于 localStorage）；无记录时为 null（未指定）。
  selectedReasoningEffort: loadPersistedSelectedReasoningEffort(),
  availableModels: [],
  modelsLoaded: false,
  // 进入应用时恢复全部任务的输入草稿（持久化于 localStorage）；无记录时为空对象。
  drafts: loadPersistedInputDrafts(),

  // --- 动作 ---

  setWorkspaceTasks: (workspaceId: number, tasks: TaskRecord[]) => {
    // 纯粹的分组写入并标记已加载。首屏活跃任务恢复由 App 层（resumeAttempted 分支）
    // 单点负责，本 action 不内嵌任何恢复/回退编排，以从结构上杜绝「展开 workspace
    // 意外切走中央对话」这一本次需求明确禁止的行为。
    const incomingTaskIds = new Set(tasks.map((task) => task.task_id));
    const removedTaskIds = Object.values(get().tasksById)
      .filter((task) => task.workspace_id === workspaceId && !incomingTaskIds.has(task.task_id))
      .map((task) => task.task_id);
    const activeTaskId = get().activeTaskId;
    const activeTaskRemoved = activeTaskId !== null && removedTaskIds.includes(activeTaskId);

    set((state) => {
      const loaded = new Set(state.loadedWorkspaceIds);
      loaded.add(workspaceId);
      const tasksById = { ...state.tasksById };
      for (const taskId of removedTaskIds) {
        delete tasksById[taskId];
      }
      for (const task of tasks) {
        tasksById[task.task_id] = task;
      }
      return {
        tasksById,
        tasksByWorkspaceId: { ...state.tasksByWorkspaceId, [workspaceId]: tasks },
        loadedWorkspaceIds: loaded,
        ...(activeTaskRemoved ? { activeTaskId: null, activeTurnId: null } : {}),
      };
    });
    if (activeTaskRemoved) {
      persistActiveTaskId(null);
    }
    for (const taskId of removedTaskIds) {
      useEventStore.getState().invalidateTask(taskId);
    }
  },

  isWorkspaceLoaded: (workspaceId: number) => {
    // workspaceId 为后端 int 主键，与 tasksByWorkspaceId/loadedWorkspaceIds 的键类型一致。
    return get().loadedWorkspaceIds.has(workspaceId);
  },

  addTask: (task: TaskRecord) => {
    // 仅当该 workspace 已加载时才把新任务插入分组；未加载分组不接收注入，
    // 否则会把「未加载（undefined）」伪造成「已加载且只有 1 条」，导致后续真实拉取
    // 被 isWorkspaceLoaded 判定跳过（见 useWorkspaceTaskLazyLoad）。未加载时仅更新
    // 活跃任务，真实列表交给随后的 ensureLoaded 拉取。
    set((state) => {
      const partial: Partial<TaskState> = {
        tasksById: { ...state.tasksById, [task.task_id]: task },
      };
      if (state.loadedWorkspaceIds.has(task.workspace_id)) {
        const group = state.tasksByWorkspaceId[task.workspace_id] ?? [];
        partial.tasksByWorkspaceId = {
          ...state.tasksByWorkspaceId,
          [task.workspace_id]: [task, ...group.filter((item) => item.task_id !== task.task_id)],
        };
      }
      return partial;
    });
    // 若尚无活跃任务，新任务自动成为活跃任务；活跃轮次由调用方（useTask）
    // 经 turnsByTask 显式设置，此处不持有任务级 turn 推导。
    if (!get().activeTaskId) {
      applyActiveTask(set, task.task_id, null);
    }
  },

  replaceTask: (oldTaskId: number, task: TaskRecord) => {
    // 同 addTask：仅已加载分组接收注入，避免伪造加载态。
    // 乐观期临时任务经 addTask 以负 number 占位键存入 tasksById/drafts（临时 id 维度），
    // 此处按同 number 占位键删除临时记录；真实记录以 number 键写入。键维度单一为 number，
    // 与 TaskRecord.task_id / Record<number> 完全对齐，无需任何 string 中间态 cast。
    // 承接 migrateDraftEntry 计算出的落盘指令，在 set 回调外统一执行
    // （set 回调内不得有副作用，持久化必须在回调外进行）。
    // 依赖约定：Zustand 的 set 回调是同步执行的，因此回调内对该变量的赋值，
    // 在下方回调外的读取处必然可见，不存在时序竞态。
    let pendingDraftMigration: DraftMigrationPersist | null = null;
    set((state) => {
      const tasksById = { ...state.tasksById };
      delete tasksById[oldTaskId];
      tasksById[task.task_id] = task;
      const partial: Partial<TaskState> = { tasksById };
      if (state.loadedWorkspaceIds.has(task.workspace_id)) {
        const group = state.tasksByWorkspaceId[task.workspace_id] ?? [];
        partial.tasksByWorkspaceId = {
          ...state.tasksByWorkspaceId,
          [task.workspace_id]: [
            task,
            ...group.filter((item) => item.task_id !== task.task_id),
          ],
        };
      }
      // 临时任务转正：把占位键上的输入草稿迁移到真实任务 ID，避免切走后丢失未发送内容。
      const migration = migrateDraftEntry(state.drafts, oldTaskId, task.task_id);
      if (migration.drafts !== undefined) {
        partial.drafts = migration.drafts;
      }
      pendingDraftMigration = migration.persist;
      return partial;
    });
    // 落盘与内存态同源于 migrateDraftEntry 的单次计算结果，避免两处各自推导产生时序竞态。
    applyDraftMigration(pendingDraftMigration);
    // 临时任务转正：活跃 ID 从 temp-x 变为真实 ID 时，必须同步持久化，
    // 否则新建任务重启后无法恢复（与 setActiveTask 共用同一不变式出口）。
    // 真实轮次 ID 由调用方（useTask）显式经 setActiveTask 设置，此处传 null。
    if (get().activeTaskId === task.task_id) {
      applyActiveTask(set, task.task_id, null);
    }
  },

  removeTask: (taskId: number) => {
    const wasActive = get().activeTaskId === taskId;
    set((state) => {
      const tasksById = { ...state.tasksById };
      delete tasksById[taskId];
      // 直接对 number 键迭代（tasksByWorkspaceId 的键是 workspace_id: number），
      // 回写 next 保持 Record<number> 类型，避免 Object.entries 引入 string 中间键再回写导致的类型冲突。
      const next: Record<number, TaskRecord[]> = {};
      for (const wsId of Object.keys(state.tasksByWorkspaceId).map(Number)) {
        next[wsId] = state.tasksByWorkspaceId[wsId]!.filter((item) => item.task_id !== taskId);
      }
      // 任务删除即清理其输入草稿，避免草稿随已删任务永久驻留存储。
      const drafts = { ...state.drafts };
      delete drafts[taskId];
      return { tasksById, tasksByWorkspaceId: next, drafts };
    });
    // 持久化草稿同步清理（持久化失败仅记 WARN 不影响内存）。
    persistInputDraft(taskId, "");
    // 删除的是当前活跃任务时同步清除持久化，避免下次启动恢复到一个已删除的任务。
    if (wasActive) {
      applyActiveTask(set, null);
    }
    // 任务移除即意味着其历史事件缓存失效，同步清除以避免幽灵 timeline。
    useEventStore.getState().invalidateTask(taskId);
    // 上下文占用缓存同理：usage 按 taskId 分键，不清理会残留无归属条目。
    useContextUsageStore.getState().resetTask(taskId);
  },

  updateTask: (taskId: number, updates: Partial<TaskRecord>) => {
    set((state) => {
      const currentTask = state.tasksById[taskId];
      const tasksById = currentTask
        ? { ...state.tasksById, [taskId]: { ...currentTask, ...updates } }
        : state.tasksById;
      // 直接对 number 键迭代（workspace_id: number），next 保持 Record<number> 类型，
      // 避免 Object.entries 的 string 中间键回写 Record<number> 产生的类型冲突。
      const next: Record<number, TaskRecord[]> = {};
      for (const wsId of Object.keys(state.tasksByWorkspaceId).map(Number)) {
        next[wsId] = state.tasksByWorkspaceId[wsId]!.map((t) => (t.task_id === taskId ? { ...t, ...updates } : t));
      }
      return { tasksById, tasksByWorkspaceId: next };
    });
  },

  setActiveTask: (taskId: number | null, turnId?: number | null) => {
    // 经统一入口设置，确保持久化与内存态一致（临时任务/清空时自动清除持久化）。
    applyActiveTask(set, taskId, turnId);
  },

  /**
   * 设置当前活跃轮次。
   *
   * @param turnId - 目标活跃轮次 ID（number，后端 int 主键；可为 null 表示清空）。
   *
   * @sideeffect 仅更新内存态 activeTurnId，不触达持久化（活跃轮次由活跃任务恢复链路间接承载）。
   */
  setActiveTurn: (turnId: number | null) => {
    set({ activeTurnId: turnId });
  },

  /**
   * 设置当前选中的模型身份；null 表示未选择（需显式选择后发送，设计 §9.1）。
   *
   * 入参为完整模型条目，二元组在此统一抽取并落库，调用方无需也不应自行拼装
   * provider_id——避免出现「只有 model_name 没有 provider_id」的残缺选择态。
   *
   * 切换/清空模型时按「安全默认」统一处理推理强度档位竞态（null 与非 null 走同一条清理路径，
   * 不因提前 return 而分叉）：
   * - model 为 null（未选择）→ 一并清除 selectedReasoningEffort 的运行时态与持久化，
   *   与「凡改 selectedModel 必同步 localStorage」契约一致，避免清空模型却残留无效档位。
   * - 传入具体模型且其 ``reasoning_effort?.supported !== true`` → 清空
   *   ``selectedReasoningEffort``（残留档位对不支持强度的模型无意义）。
   *
   * @param model - 选中的模型条目（来自 availableModels）；null 表示清空选择（同时清档位）。
   *
   * @sideeffect
   * - 同步持久化 selectedModel（见 persistSelectedModel 契约）。
   *   model 为 null 时同样会持久化清除 selectedModel。
   * - 凡不支持强度的模型（含 null）一律清空并持久化清除 selectedReasoningEffort
   *   （安全默认，消除跨模型/清空残留档位竞态）。
   */
  setSelectedModel: (model: ModelEntryRecord | null) => {
    // 二元组在本出口统一抽取：provider_id 与 model_name 永远同时写入，
    // 结构上杜绝创建轮次时「有 model_name 无 provider_id」被后端判 400。
    const nextSelection: SelectedModel | null =
      model === null ? null : { provider_id: model.provider_id, model_name: model.model_name };
    set({ selectedModel: nextSelection });
    // 单一持久化出口：内存态变更必须同步落 localStorage（见 persistSelectedModel 契约）。
    persistSelectedModel(nextSelection);
    // 档位联动走共享规则：传入条目本身即事实源，无需回查 availableModels。
    const nextEffort = resolveEffortAfterModelChange(model, get().selectedReasoningEffort);
    if (nextEffort !== get().selectedReasoningEffort) {
      set({ selectedReasoningEffort: nextEffort });
      persistSelectedReasoningEffort(nextEffort);
    }
  },

  setSelectedReasoningEffort: (effort: string | null) => {
    set({ selectedReasoningEffort: effort });
    // 单一持久化出口：内存态变更必须同步落 localStorage（见 persistSelectedReasoningEffort 契约）。
    persistSelectedReasoningEffort(effort);
  },

  /**
   * 从后端拉取可用模型列表并写入 store（幂等，可重复调用刷新）。
   * 失败仅记录 WARN 日志，不抛出、不影响主流程。
   *
   * 列表加载成功后对当前选中的模型做「档位校准」双保险：若
   * ``selectedModel`` 指向的当前模型已加载且 ``reasoning_effort?.supported !== true``
   * 但 ``selectedReasoningEffort`` 非空，则清空残留档位（覆盖「切模型时
   * availableModels 未加载、当时保守清空、但后端实际不支持强度」的竞态残留）。
   * 该清空仅在「查到当前模型且不支持强度但档位非空」时触发，不会误伤支持强度的模型。
   *
   * @returns 加载成功返回 true；请求失败返回 false（旧缓存保留、modelsLoaded 复位）。
   *
   * @sideeffect
   * - set availableModels / modelsLoaded（成功）；失败仅复位 modelsLoaded 并保留旧缓存。
   * - 加载成功后可能清空并持久化清除 selectedReasoningEffort（当当前模型不支持强度但档位残留时）。
   */
  refreshAvailableModels: async () => {
    try {
      const models = await listModels();
      set({ availableModels: models, modelsLoaded: true });
      // 双保险校准：列表到齐后，若当前选中模型不支持强度但仍有残留档位，则清空。
      // 与 setSelectedModel 共用同一条规则（resolveEffortAfterModelChange），
      // 覆盖「切模型时列表未加载、当时保守清空、但后端实际不支持强度」的竞态残留。
      const currentSelection = get().selectedModel;
      const currentModel = findModelBySelection(models, currentSelection);
      const nextEffort = resolveEffortAfterModelChange(currentModel, get().selectedReasoningEffort);
      if (nextEffort !== get().selectedReasoningEffort) {
        set({ selectedReasoningEffort: nextEffort });
        persistSelectedReasoningEffort(nextEffort);
      }
      return true;
    } catch (err) {
      logWarn("刷新可用模型列表失败", {
        module: "taskStore",
        error: err instanceof Error ? err.message : String(err),
      });
      // 保留旧缓存（availableModels 不覆盖），仅复位加载标志，避免空数组与未加载态混淆。
      set({ modelsLoaded: false });
      return false;
    }
  },

  /**
   * 清空全部任务（按工作区分组、实体、加载标记、活跃 turn、草稿均复位）。
   *
   * 与「清空/切换模型即清档位」的一致性对齐：清空任务即清空整个会话上下文，因此一并复位
   * ``selectedModel`` / ``selectedReasoningEffort`` 的运行时态并同步持久化清除，
   * 避免这些选择态被持久化跨会话残留（下次启动恢复到一个已无意义的选择）。
   *
   * @sideeffect
   * - set tasksById / tasksByWorkspaceId / loadedWorkspaceIds / activeTurnId / drafts 为空。
   * - 经 applyActiveTask 同步清除活跃任务持久化。
   * - 经 localStorage.removeItem 清除输入草稿持久化。
   * - 复位 selectedModel / selectedReasoningEffort 并同步持久化清除（见 persist 契约）。
   * - 清空全部任务的上下文占用缓存（usageByTaskId）。
   */
  clearTasks: () => {
    set({
      tasksById: {},
      tasksByWorkspaceId: {},
      loadedWorkspaceIds: new Set(),
      activeTurnId: null,
      drafts: {},
      selectedModel: null,
      selectedReasoningEffort: null,
    });
    // 清空活跃任务须同步清除持久化，避免下次启动恢复到一个已不存在的任务。
    applyActiveTask(set, null);
    // 清空全部任务即清空所有输入草稿持久化（避免下次启动恢复孤儿草稿）。
    try {
      localStorage.removeItem(INPUT_DRAFTS_STORAGE_KEY);
    } catch (err) {
      logWarn("清空输入草稿持久化失败", { module: "taskStore", error: err instanceof Error ? err.message : String(err) });
    }
    // 清空任务即清空会话上下文，一并复位模型/档位选择态并同步持久化清除，避免跨会话残留。
    persistSelectedModel(null);
    persistSelectedReasoningEffort(null);
    // 上下文占用缓存按 taskId 分键，清空全部任务后无一条仍属有效任务。
    useContextUsageStore.getState().clearAll();
  },

  getWorkspaceTaskIds: (workspaceId: number) => {
    const groupedTaskIds = (get().tasksByWorkspaceId[workspaceId] ?? []).map((task) => task.task_id);
    const entityTaskIds = Object.values(get().tasksById)
      .filter((task) => task.workspace_id === workspaceId)
      .map((task) => task.task_id);
    // 两个来源都可能有对方没有的 id（分组懒加载 / 任务已打开但未归入分组），取并集。
    return [...new Set([...groupedTaskIds, ...entityTaskIds])];
  },

  clearWorkspaceTasks: (workspaceId: number) => {
    // workspaceId 为后端 int 主键，与 tasksByWorkspaceId/loadedWorkspaceIds 的键类型一致。
    // 级联清理：被删工作区下的任务事件缓存失效，且若活跃任务恰在该工作区则清空活跃态，
    // 避免 activeTaskId 悬空指向已删工作区的任务（与 removeTask 行为对齐，不泄漏到组件层）。
    // 活跃任务归属判定直接基于 activeTaskId 的 workspace（经 getTaskById），不依赖分组是否
    // 加载——未展开过的 workspace 也照样能清掉幽灵活跃态与事件缓存。
    const removedTaskIds = get().getWorkspaceTaskIds(workspaceId);
    const activeId = get().activeTaskId;
    const activeTask = activeId ? get().tasksById[activeId] : undefined;
    const wasActiveInWorkspace = activeTask?.workspace_id === workspaceId;
    set((state) => {
      const next = { ...state.tasksByWorkspaceId };
      delete next[workspaceId];
      const tasksById = { ...state.tasksById };
      for (const taskId of removedTaskIds) {
        delete tasksById[taskId];
      }
      const loaded = new Set(state.loadedWorkspaceIds);
      loaded.delete(workspaceId);
      return { tasksById, tasksByWorkspaceId: next, loadedWorkspaceIds: loaded };
    });
    for (const taskId of removedTaskIds) {
      useEventStore.getState().invalidateTask(taskId);
      useContextUsageStore.getState().resetTask(taskId);
    }
    if (wasActiveInWorkspace) {
      applyActiveTask(set, null);
    }
  },

  getTaskById: (taskId: number) => {
    return get().tasksById[taskId];
  },

  setInputDraft: (taskId: number, draft: string) => {
    // 单一出口：内存态与持久化同步更新；持久化失败仅记 WARN 不影响内存。
    set((state) => {
      const drafts = { ...state.drafts };
      if (draft.length > 0) {
        drafts[taskId] = draft;
      } else {
        delete drafts[taskId];
      }
      return { drafts };
    });
    persistInputDraft(taskId, draft);
  },
}));

// ---------- 派生选择器 ----------

/**
 * 获取当前活跃任务的完整记录。
 * @returns 活跃任务对象或 undefined。
 */
export const selectActiveTask = (state: TaskState): TaskRecord | undefined => {
  if (!state.activeTaskId) return undefined;
  return state.tasksById[state.activeTaskId];
};

/**
 * 获取当前活跃任务的运行状态。
 * @returns 任务状态字符串或 null。
 */
export const selectActiveTaskStatus = (state: TaskState): TaskStatus | null => {
  if (!state.activeTaskId) return null;
  const task = state.tasksById[state.activeTaskId];
  return task?.execution_status ?? task?.status ?? null;
};
