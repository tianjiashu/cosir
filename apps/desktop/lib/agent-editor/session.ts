import { createStore } from "zustand/vanilla";

import type { AgentConfiguration, AgentConfigurationInput } from "@/lib/api/configuration";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { modelSettingsFromForm, modelSettingsToForm, type ModelSettingsForm } from "@/lib/model-settings-form";

export type AgentConfigurationScope = "workspace" | "system";

/** 表单值：数字与模型参数保持输入框原文，保存时再统一校验转换。 */
type AgentEditorForm = Omit<AgentConfigurationInput, "max_steps" | "model_settings"> & {
  max_steps: string;
  model_settings: ModelSettingsForm;
};

/** 本次保存的目标：`agentId` 为 null 时 create，否则 update 这份已保存的配置。 */
export type PersistAgent = (
  input: AgentConfigurationInput,
  target: { scope: AgentConfigurationScope; agentId: string | null },
) => Promise<AgentConfiguration>;

type AgentEditorState = {
  form: AgentEditorForm;
  /** 保存范围；首存后与 Agent ID 一起锁定。 */
  scope: AgentConfigurationScope;
  /** 最近一次保存成功的表单；null 表示尚未保存过。 */
  baseline: AgentEditorForm | null;
  /** 已保存配置的 Agent ID；非 null 后再保存走 update。 */
  savedAgentId: string | null;
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
 * 一次子 Agent 配置编辑的唯一 working copy owner，与 `TeamEditorSession` 同形：
 * 配置页持有到离开编辑器，工作台草稿标签持有到关闭标签。
 *
 * - `initial` 为已有配置时 `saved` 为 true：以它为基线，首存即 update。
 * - 工具生成的草稿（`saved: false` 且有 `initial`）从一开始就是未保存内容；空白新建在真正修改前不算未保存。
 * - 保存应答只推进提交时那份快照的基线，期间产生的新编辑保留并继续算作未保存。
 * - 同一时刻只允许一次保存，进行中的重复调用直接返回 false；保存期间范围不可改。
 * - 校验失败或持久化失败直接抛出，由调用方的 mutation 呈现；会话除 `saving` 外不变。
 */
export function createAgentEditorSession(options: {
  initial: Partial<AgentConfigurationInput> | null;
  saved: boolean;
  scope: AgentConfigurationScope;
}) {
  const form = editorForm(options.initial);
  const baseline = options.saved ? form : null;
  const blank = options.initial === null ? JSON.stringify({ form, scope: options.scope }) : null;
  const dirtyAgainst = (next: AgentEditorForm, scope: AgentConfigurationScope, saved: AgentEditorForm | null) => (
    saved ? JSON.stringify(next) !== JSON.stringify(saved) : JSON.stringify({ form: next, scope }) !== blank
  );
  const store = createStore<AgentEditorState>(() => ({
    form,
    scope: options.scope,
    baseline,
    savedAgentId: options.saved ? form.agent_id : null,
    dirty: dirtyAgainst(form, options.scope, baseline),
    saving: false,
  }));

  return {
    store,
    setField<K extends keyof AgentEditorForm>(key: K, value: AgentEditorForm[K]) {
      const state = store.getState();
      const next = { ...state.form, [key]: value };
      store.setState({ form: next, dirty: dirtyAgainst(next, state.scope, state.baseline) });
    },
    /** 保存中或已保存后范围锁定，调用不生效（视图同时禁用控件）。 */
    setScope(scope: AgentConfigurationScope) {
      const state = store.getState();
      if (state.saving || state.savedAgentId !== null) return;
      store.setState({ scope, dirty: dirtyAgainst(state.form, scope, state.baseline) });
    },
    /** 返回 false 表示已有保存在进行、本次未发起。 */
    async save(persist: PersistAgent): Promise<boolean> {
      const before = store.getState();
      if (before.saving) return false;
      const submitted = before.form;
      const input = inputFromForm(submitted);
      store.setState({ saving: true });
      try {
        const saved = await persist(input, { scope: before.scope, agentId: before.savedAgentId });
        store.setState((current) => ({
          baseline: submitted,
          savedAgentId: saved.agent_id,
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
