import { createStore } from "zustand/vanilla";

import type { ConfigurationScope } from "@/lib/api/configuration-scope";
import type { AgentConfiguration, AgentConfigurationInput } from "@/lib/api/configuration";
import { saveResolvingConflicts } from "@/lib/configuration/revision-conflict";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { modelSettingsFromForm, modelSettingsToForm, type ModelSettingsForm } from "@/lib/agent-editor/model-settings-form";

/** 表单值：数字与模型参数保持输入框原文，保存时再统一校验转换。 */
type AgentEditorForm = Omit<AgentConfigurationInput, "max_steps" | "model_settings"> & {
  max_steps: string;
  model_settings: ModelSettingsForm;
};

/** 本次保存的目标：`agentId` 为 null 时 create，否则以 `baseRevision` 为前置条件 update 这份已保存的配置。 */
export type PersistAgent = (
  input: AgentConfigurationInput,
  target: { scope: ConfigurationScope; agentId: string | null; baseRevision: string | null },
) => Promise<AgentConfiguration>;

/** 冲突后选「载入外部版本」时重读这份配置的最新内容。 */
export type LoadLatestAgent = (target: { scope: ConfigurationScope; agentId: string }) => Promise<AgentConfiguration>;

type AgentEditorState = {
  form: AgentEditorForm;
  /** 保存范围；首存后与 Agent ID 一起锁定。 */
  scope: ConfigurationScope;
  /** 最近一次保存成功的表单；null 表示尚未保存过。 */
  baseline: AgentEditorForm | null;
  /** 已保存配置的 Agent ID；非 null 后再保存走 update。 */
  savedAgentId: string | null;
  /** 基线对应的文件版本：update 的前置条件，只随保存成功与载入外部版本推进。 */
  revision: string | null;
  dirty: boolean;
  /** 保存请求进行中：挡住重复保存与范围修改。放在会话而非视图里，面板卸载重挂也不会丢。 */
  saving: boolean;
};

function editorForm(initial: Partial<AgentConfigurationInput> | null): AgentEditorForm {
  return {
    agent_id: initial?.agent_id ?? "",
    role: initial?.role ?? "",
    description: initial?.description ?? "",
    system_prompt: initial?.system_prompt ?? "",
    allowed_tool_groups: initial?.allowed_tool_groups ?? [],
    max_steps: String(initial?.max_steps ?? 100),
    model_config_id: initial?.model_config_id ?? null,
    model_settings: modelSettingsToForm(initial?.model_settings),
  };
}

function inputFromForm(form: AgentEditorForm): AgentConfigurationInput {
  // 空输入框不能按 Number("") 变成 0 静默提交；后端只要求正整数。
  const maxSteps = Number(form.max_steps.trim());
  if (!form.max_steps.trim() || !Number.isInteger(maxSteps) || maxSteps <= 0) {
    throw new UserFacingError("最大步数必须是正整数");
  }
  return { ...form, max_steps: maxSteps, model_settings: modelSettingsFromForm(form.model_settings) };
}

/**
 * 一次子 Agent 配置编辑的唯一 working copy owner：
 * 配置页持有到离开编辑器，工作台草稿标签持有到关闭标签。
 *
 * - `initial` 为已有配置时 `saved` 带它的文件版本：以它为基线，首存即 update，版本作为前置条件。
 * - 工具生成的草稿（`saved: false` 且有 `initial`）从一开始就是未保存内容；空白新建在真正修改前不算未保存。
 * - 保存应答只推进提交时那份快照的基线，期间产生的新编辑保留并继续算作未保存。
 * - 同一时刻只允许一次保存，进行中的重复调用直接返回 false；身份（Agent ID 与范围）自首存开始锁定，其余字段保存期间照常可改。
 * - 文件在编辑期间被别处改过（配置页与工作台草稿写同一份文件，也可能被手改）时，由用户选覆盖或载入外部版本；
 *   载入会以最新配置重置表单与基线。
 * - 校验失败、取消冲突或持久化失败直接抛出，由调用方的 mutation 呈现；会话除 `saving` 外不变。
 */
export function createAgentEditorSession(options: {
  initial: Partial<AgentConfigurationInput> | null;
  /** 已保存过的配置给出它的文件版本；工具生成的草稿与空白新建为 false。 */
  saved: false | { revision: string | null };
  scope: ConfigurationScope;
}) {
  const form = editorForm(options.initial);
  const baseline = options.saved ? form : null;
  const blank = options.initial === null ? JSON.stringify({ form, scope: options.scope }) : null;
  const dirtyAgainst = (next: AgentEditorForm, scope: ConfigurationScope, saved: AgentEditorForm | null) => (
    saved ? JSON.stringify(next) !== JSON.stringify(saved) : JSON.stringify({ form: next, scope }) !== blank
  );
  const store = createStore<AgentEditorState>(() => ({
    form,
    scope: options.scope,
    baseline,
    savedAgentId: options.saved ? form.agent_id : null,
    revision: options.saved ? options.saved.revision : null,
    dirty: dirtyAgainst(form, options.scope, baseline),
    saving: false,
  }));

  return {
    store,
    /**
     * Agent ID 与范围同属身份：保存中或已保存后改不动（视图同时禁用控件）。首存期间改了它，回执锁定的是
     * 已提交的 ID 而表单显示新值，之后的 update 就会以旧 ID 寻址、请求体却带新 ID。
     */
    setField<K extends keyof AgentEditorForm>(key: K, value: AgentEditorForm[K]) {
      const state = store.getState();
      if (key === "agent_id" && (state.saving || state.savedAgentId !== null)) return;
      const next = { ...state.form, [key]: value };
      store.setState({ form: next, dirty: dirtyAgainst(next, state.scope, state.baseline) });
    },
    /** 保存中或已保存后范围锁定，调用不生效（视图同时禁用控件）。 */
    setScope(scope: ConfigurationScope) {
      const state = store.getState();
      if (state.saving || state.savedAgentId !== null) return;
      store.setState({ scope, dirty: dirtyAgainst(state.form, scope, state.baseline) });
    },
    /** 返回 false 表示没有写入：已有保存在进行、本次未发起，或冲突后选了载入外部版本。 */
    async save(persist: PersistAgent, loadLatest: LoadLatestAgent): Promise<boolean> {
      const before = store.getState();
      if (before.saving) return false;
      const submitted = before.form;
      const input = inputFromForm(submitted);
      const { scope, savedAgentId: agentId } = before;
      store.setState({ saving: true });
      try {
        const result = await saveResolvingConflicts(
          `子 Agent「${submitted.agent_id}」`,
          before.revision,
          (baseRevision) => persist(input, { scope, agentId, baseRevision }),
        );
        if (result === "reload") {
          // 只有 update 会冲突，此时 agentId 必已存在。
          const latest = await loadLatest({ scope, agentId: agentId! });
          const latestForm = editorForm(latest);
          store.setState({ form: latestForm, baseline: latestForm, revision: latest.revision, dirty: false });
          return false;
        }
        const { saved } = result;
        store.setState((current) => ({
          baseline: submitted,
          savedAgentId: saved.agent_id,
          revision: saved.revision,
          dirty: JSON.stringify(current.form) !== JSON.stringify(submitted),
        }));
        return true;
      } finally {
        store.setState({ saving: false });
      }
    },
  };
}

export type AgentEditorSession = ReturnType<typeof createAgentEditorSession>;
