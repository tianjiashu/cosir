"use client";

import { useEffect, useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2Icon, EyeIcon, EyeOffIcon, Loader2Icon, Settings2Icon, Trash2Icon, XCircleIcon } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/state-panel";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  createModelConfig,
  deleteModelConfig,
  discoverModelConfigs,
  getModelConfigs,
  testDraftModelConfig,
  updateModelConfig,
  type ModelConfig,
  type ModelConfigTestInput,
} from "@/lib/api/model-configs";
import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { confirmDiscardUnsaved, useUnsavedChanges } from "@/lib/unsaved-changes";

type FormState = {
  configName: string;
  baseUrl: string;
  apiKey: string;
  modelName: string;
  contextWindowK: string;
  supportsThinking: boolean;
  supportsReasoningEffort: boolean;
  supportsImage: boolean;
};

const emptyForm: FormState = {
  configName: "",
  baseUrl: "",
  apiKey: "",
  modelName: "",
  contextWindowK: "128",
  supportsThinking: false,
  supportsReasoningEffort: false,
  supportsImage: false,
};

function toForm(config: ModelConfig): FormState {
  return {
    configName: config.config_name,
    baseUrl: config.base_url,
    apiKey: config.api_key,
    modelName: config.model_name,
    contextWindowK: String(config.context_window_k),
    supportsThinking: config.supports_thinking,
    supportsReasoningEffort: config.supports_reasoning_effort,
    supportsImage: config.supports_image,
  };
}

function toInput(form: FormState): ModelConfigTestInput {
  return {
    config_name: form.configName.trim(),
    base_url: form.baseUrl.trim(),
    api_key: form.apiKey.trim(),
    model_name: form.modelName.trim(),
    context_window_k: Number(form.contextWindowK),
    supports_thinking: form.supportsThinking,
    supports_reasoning_effort: form.supportsReasoningEffort,
    supports_image: form.supportsImage,
  };
}

/**
 * 模型连接配置的增删改与连接测试。
 *
 * 保存前必须对「当前表单内容」测试通过：测试结果按表单签名绑定，任何字段改动都会使其失效。
 * 列表经 TanStack Query 读取；输入框旁的模型选择目录（lib/model-catalog）是独立的共享缓存，
 * 由调用方在对话框关闭时通过 onChanged 刷新，本组件不直接写它。
 */
export function ModelConfigPanel({
  onChanged,
  onBeforeOpen,
}: {
  onChanged: () => void;
  onBeforeOpen?: () => void;
}) {
  const queryClient = useQueryClient();
  const saveHintId = useId();
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<FormState>(emptyForm);
  // 最近一次载入或保存的表单内容，用于判断是否有未保存修改。
  const [baseline, setBaseline] = useState<FormState>(emptyForm);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [showApiKey, setShowApiKey] = useState(false);
  const [discoveredModels, setDiscoveredModels] = useState<string[] | null>(null);
  const [discoveryHint, setDiscoveryHint] = useState<string | null>(null);

  const formInput = toInput(form);
  const formSignature = JSON.stringify(formInput);
  const formComplete = Boolean(
    formInput.config_name &&
      formInput.base_url &&
      formInput.api_key &&
      formInput.model_name &&
      Number.isInteger(formInput.context_window_k) &&
      formInput.context_window_k > 0,
  );
  const dirty = formSignature !== JSON.stringify(toInput(baseline));
  useUnsavedChanges("settings", "模型配置", open && dirty);

  const configsQuery = useQuery({
    queryKey: queryKeys.modelConfigs(),
    queryFn: ({ signal }) => getModelConfigs({ signal }),
    enabled: open,
  });

  const testMutation = useMutation({ mutationFn: testDraftModelConfig });
  // 测试结果只对发起测试时的表单内容有效；表单改动后既不展示旧结果，也不允许保存。
  const testIsCurrent = testMutation.variables !== undefined
    && JSON.stringify(testMutation.variables) === formSignature;
  const testPassed = formComplete && testIsCurrent && testMutation.data?.success === true;

  const saveMutation = useMutation({
    mutationFn: ({ configId, input }: { configId: number | null; input: ModelConfigTestInput; form: FormState }) => {
      if (configId === null) return createModelConfig(input);
      const { api_key, ...savedFields } = input;
      return updateModelConfig(configId, { ...savedFields, ...(api_key ? { api_key } : {}) });
    },
    onSuccess: (savedConfig, { configId, form: savedForm }) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.modelConfigs() });
      if (configId === null) setEditingId(savedConfig.config_id);
      setBaseline(savedForm);
      setShowApiKey(false);
      notify.success("模型配置已保存");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deleteModelConfig,
    onSuccess: (_result, configId) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.modelConfigs() });
      // 被删除的配置正在编辑时清空表单，否则后续保存会对已删除的 id 发起更新。
      if (editingId === configId) resetForm();
      notify.success("模型配置已删除");
    },
  });

  useEffect(() => {
    if (!open) {
      setDiscoveredModels(null);
      setDiscoveryHint(null);
      return;
    }

    const baseUrl = form.baseUrl.trim();
    const apiKey = form.apiKey.trim();
    setDiscoveredModels(null);
    setDiscoveryHint(null);
    if (!baseUrl || !apiKey) return;

    const controller = new AbortController();
    let active = true;
    const timer = window.setTimeout(() => {
      void discoverModelConfigs(
        { base_url: baseUrl, api_key: apiKey },
        { signal: controller.signal },
      )
        .then((result) => {
          if (!active) return;
          if (result.models.length === 0) {
            setDiscoveryHint("接口未返回可选模型，请手动填写模型名称");
            return;
          }
          const currentModel = form.modelName.trim();
          const models = currentModel && !result.models.includes(currentModel)
            ? [currentModel, ...result.models]
            : result.models;
          setDiscoveredModels(models);
        })
        .catch((cause) => {
          if (!active || (cause instanceof DOMException && cause.name === "AbortError")) return;
          // 模型目录发现是表单增强能力；失败时提示改为手动填写，不阻断配置流程。
          setDiscoveryHint("未能自动获取模型列表，请手动填写模型名称");
          void frontendLog("WARNING", "model_config_discovery_failed", "模型目录自动发现失败，回退为手动填写", {
            data: { base_url: baseUrl },
            error: cause,
          });
        });
    }, 450);

    return () => {
      active = false;
      window.clearTimeout(timer);
      controller.abort();
    };
    // 只按连接参数重新发现；模型名改动不应触发请求，读取的是发起时的取值。
  }, [form.apiKey, form.baseUrl, open]);

  function loadForm(next: FormState, configId: number | null) {
    setForm(next);
    setBaseline(next);
    setEditingId(configId);
    setShowApiKey(false);
    testMutation.reset();
    saveMutation.reset();
  }

  function resetForm() {
    loadForm(emptyForm, null);
  }

  async function closeDialog() {
    if (!(await confirmDiscardUnsaved("settings"))) return;
    // 放弃的修改不应在下次打开时重新出现。
    setForm(baseline);
    setOpen(false);
    onChanged();
  }

  function save() {
    if (!testPassed || saveMutation.isPending) return;
    saveMutation.mutate({ configId: editingId, input: formInput, form });
  }

  function testDraft() {
    if (!formComplete || testMutation.isPending) return;
    testMutation.mutate(formInput);
  }

  async function remove(config: ModelConfig) {
    await requestConfirmation({
      title: "删除模型配置？",
      description: `确定删除模型配置「${config.config_name}」吗？使用该配置的对话将无法继续选择它。`,
      confirmLabel: "删除",
      busyLabel: "正在删除…",
      tone: "destructive",
      icon: "delete",
      action: () => deleteMutation.mutateAsync(config.config_id),
      failureFallback: "删除失败，请重试",
    });
  }

  const busy = saveMutation.isPending || deleteMutation.isPending;
  const testOutcome = testIsCurrent && !testMutation.isPending
    ? testMutation.isError
      ? { ok: false, text: userErrorMessage(testMutation.error, "连接测试失败，请重试") }
      : testMutation.data
        ? {
            ok: testMutation.data.success,
            text: testMutation.data.success
              ? `连接成功${testMutation.data.elapsed_ms ? ` · ${testMutation.data.elapsed_ms}ms` : ""}`
              : testMutation.data.error_message ?? "连接失败，请检查配置",
          }
        : null
    : null;
  const saveHint = testPassed || testMutation.isPending
    ? null
    : formComplete
      ? "请先测试连接，通过后才能保存"
      : "填写全部连接信息并测试连接通过后才能保存";

  return (
    <>
      <button
        type="button"
        onClick={() => {
          onBeforeOpen?.();
          setOpen(true);
        }}
        className="text-muted-foreground hover:text-foreground inline-flex h-7 items-center gap-1 rounded-md px-2 text-xs"
      >
        <Settings2Icon className="size-3.5" />
        模型设置
      </button>
      <Dialog
        open={open}
        onOpenChange={(nextOpen) => {
          if (nextOpen) setOpen(true);
          else void closeDialog();
        }}
      >
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>模型设置</DialogTitle>
            <DialogDescription>填写 OpenAI 兼容接口的连接配置，模型会出现在选择列表中。</DialogDescription>
          </DialogHeader>

          <div className="max-h-[min(52vh,28rem)] space-y-2 overflow-y-auto">
            {configsQuery.isPending ? (
              <LoadingState label="正在读取模型配置…" size="compact" />
            ) : configsQuery.isError ? (
              <ErrorState
                message={userErrorMessage(configsQuery.error, "模型配置加载失败，请重试")}
                onRetry={() => void configsQuery.refetch()}
              />
            ) : configsQuery.data.length === 0 ? (
              <EmptyState title="暂无已配置模型" className="py-4" />
            ) : null}
            {configsQuery.data?.map((config) => (
              <div key={config.config_id} className="border-border/60 flex items-center justify-between rounded-md border p-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    {config.config_name}
                    {config.enabled && config.api_key_configured ? <CheckCircle2Icon className="text-success size-4" /> : <XCircleIcon className="text-muted-foreground size-4" />}
                  </div>
                  <p className="text-muted-foreground truncate text-xs">{config.model_name} · {config.context_window_k}K · {config.base_url}</p>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <Button variant="ghost" size="sm" disabled={busy} onClick={() => loadForm(toForm(config), config.config_id)}>
                    编辑
                  </Button>
                  <Button variant="ghost" size="icon-sm" disabled={busy} onClick={() => void remove(config)} aria-label={`删除 ${config.config_name}`}>
                    <Trash2Icon />
                  </Button>
                </div>
              </div>
            ))}
          </div>

          <div className="border-border/60 space-y-3 rounded-md border p-3">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium">{editingId === null ? "添加模型配置" : "编辑模型配置"}</h3>
              {editingId !== null && <Button variant="ghost" size="sm" onClick={resetForm}>取消编辑</Button>}
            </div>
            <label className="grid min-w-0 gap-1 text-xs">配置名称<Input value={form.configName} onChange={(event) => setForm({ ...form, configName: event.target.value })} placeholder="例如：DeepSeek 官方" /></label>
            <label className="grid min-w-0 gap-1 text-xs">API Base URL<Input value={form.baseUrl} onChange={(event) => setForm({ ...form, baseUrl: event.target.value })} placeholder="https://api.example.com/v1" /></label>
            <label className="grid min-w-0 gap-1 text-xs">API Key<div className="relative min-w-0"><Input type={showApiKey ? "text" : "password"} className="pr-9" value={form.apiKey} onChange={(event) => setForm({ ...form, apiKey: event.target.value })} placeholder="请输入 API Key" /><button type="button" className="text-muted-foreground hover:text-foreground absolute inset-y-0 right-0 inline-flex w-8 items-center justify-center" onClick={() => setShowApiKey((visible) => !visible)} aria-label={showApiKey ? "隐藏 API Key" : "显示 API Key"}>{showApiKey ? <EyeOffIcon className="size-4" /> : <EyeIcon className="size-4" />}</button></div></label>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="grid gap-1 text-xs">
                模型名称
                {discoveredModels ? (
                  <NativeSelect
                    value={form.modelName}
                    onChange={(event) => setForm({ ...form, modelName: event.target.value })}
                  >
                    <option value="">请选择模型</option>
                    {discoveredModels.map((model) => <option key={model} value={model}>{model}</option>)}
                  </NativeSelect>
                ) : (
                  <Input value={form.modelName} onChange={(event) => setForm({ ...form, modelName: event.target.value })} placeholder="例如：deepseek-chat" />
                )}
                {discoveryHint && <span className="text-muted-foreground">{discoveryHint}</span>}
              </label>
              <label className="grid min-w-0 gap-1 text-xs">上下文窗口（K）<Input type="number" min="1" step="1" value={form.contextWindowK} onChange={(event) => setForm({ ...form, contextWindowK: event.target.value })} placeholder="例如：128" /></label>
            </div>
            <div className="grid gap-2 sm:grid-cols-3">
              <label className="flex items-center gap-2 text-xs"><Checkbox checked={form.supportsThinking} onChange={(event) => setForm({ ...form, supportsThinking: event.target.checked })} />支持思考</label>
              <label className="flex items-center gap-2 text-xs"><Checkbox checked={form.supportsReasoningEffort} onChange={(event) => setForm({ ...form, supportsReasoningEffort: event.target.checked })} />支持推理强度</label>
              <label className="flex items-center gap-2 text-xs"><Checkbox checked={form.supportsImage} onChange={(event) => setForm({ ...form, supportsImage: event.target.checked })} />支持图片输入</label>
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              <Button variant="outline" disabled={busy || testMutation.isPending || !formComplete} onClick={testDraft}>{testMutation.isPending && <Loader2Icon className="animate-spin" />}测试连接</Button>
              <Button disabled={busy || testMutation.isPending || !testPassed} onClick={save} aria-describedby={saveHint ? saveHintId : undefined}>{saveMutation.isPending && <Loader2Icon className="animate-spin" />}保存配置</Button>
            </div>
            {saveHint && <p id={saveHintId} className="text-muted-foreground text-xs">{saveHint}</p>}
          </div>

          {testOutcome && <Alert tone={testOutcome.ok ? "success" : "destructive"}>{testOutcome.text}</Alert>}
          {saveMutation.isError && (
            <Alert tone="destructive">
              {/* 后端对可纠正的拒绝（如 409 名称重复）会给出明确原因，必须展示，否则用户无从知道该改什么。 */}
              {userErrorMessage(saveMutation.error, "保存失败，请检查配置后重试")}
            </Alert>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
