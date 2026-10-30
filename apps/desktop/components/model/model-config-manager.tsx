"use client";

import { useEffect, useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2Icon, EyeIcon, EyeOffIcon, Loader2Icon, Trash2Icon, XCircleIcon } from "lucide-react";

import { ClaudeSubscriptionConnection, ClaudeSubscriptionModelSelect } from "@/components/model/claude-subscription-connection";
import { useClaudeSubscription } from "@/components/model/use-claude-subscription";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/state-panel";
import {
  createModelConfig,
  deleteModelConfig,
  discoverModelConfigs,
  getModelConfigs,
  testDraftModelConfig,
  updateModelConfig,
  type ModelConfig,
  type ModelProvider,
  type ModelConfigTestInput,
} from "@/lib/api/model-configs";
import { confirmDelete } from "@/lib/confirm/confirm-store";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { useUnsavedChanges, type UnsavedScope } from "@/lib/unsaved-changes";
import { cn } from "@/lib/utils";

import { changeProvider, emptyForm, selectClaudeModel, toForm, toInput, type FormState } from "@/lib/model-config-form";

/**
 * 模型连接配置的增删改与连接测试，供 composer 的模型设置对话框与配置中心「模型」标签共用。
 *
 * - 保存前必须对「当前表单内容」测试通过：测试结果按表单签名绑定，任何字段改动都会使其失效。
 * - 编辑状态随组件卸载丢弃：对话框关闭（内容卸载）或离开配置路由即放弃未保存修改，
 *   离开前的确认由宿主按 `unsavedScope` 负责（对话框自行确认，配置中心走路由拦截）。
 * - 列表经 TanStack Query 读取；模型选择目录（lib/model-catalog）共用同一个 query key，
 *   保存 / 删除后的失效会同时刷新目录，宿主无需另行通知。
 */
export function ModelConfigManager({
  unsavedScope,
  variant,
}: {
  unsavedScope: UnsavedScope;
  /** dialog：列表限高滚动；page：配置中心面板，随页面滚动。 */
  variant: "dialog" | "page";
}) {
  const queryClient = useQueryClient();
  const saveHintId = useId();
  const [form, setForm] = useState<FormState>(emptyForm);
  // 最近一次载入或保存的表单内容，用于判断是否有未保存修改。
  const [baseline, setBaseline] = useState<FormState>(emptyForm);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [showApiKey, setShowApiKey] = useState(false);
  const [discoveredModels, setDiscoveredModels] = useState<string[] | null>(null);
  const [discoveryHint, setDiscoveryHint] = useState<string | null>(null);

  const testMutation = useMutation({
    mutationFn: ({ input }: { input: ModelConfigTestInput; authVersion: number }) => testDraftModelConfig(input),
  });
  const claude = useClaudeSubscription(form.provider === "claude_subscription", () => testMutation.reset());
  const connectionReady = form.provider !== "claude_subscription" || (claude.connected && !claude.busy);
  const formInput = toInput(form);
  const formSignature = JSON.stringify(formInput);
  const formComplete = Boolean(
    connectionReady && formInput.config_name &&
      formInput.base_url &&
      (formInput.provider === "claude_subscription" || formInput.api_key) &&
      formInput.model_name &&
      Number.isInteger(formInput.context_window_k) &&
      formInput.context_window_k > 0,
  );
  const dirty = formSignature !== JSON.stringify(toInput(baseline));
  useUnsavedChanges(unsavedScope, "模型配置", dirty);

  const configsQuery = useQuery({
    queryKey: queryKeys.modelConfigs(),
    queryFn: ({ signal }) => getModelConfigs({ signal }),
  });

  // 测试结果只对发起测试时的表单内容有效；表单改动后既不展示旧结果，也不允许保存。
  const testIsCurrent = testMutation.variables !== undefined
    && JSON.stringify(testMutation.variables.input) === formSignature
    && (form.provider !== "claude_subscription" || testMutation.variables.authVersion === claude.authVersion);
  const testPassed = formComplete && testIsCurrent && testMutation.data?.success === true;

  const saveMutation = useMutation({
    mutationFn: ({ configId, input }: { configId: number | null; input: ModelConfigTestInput; form: FormState }) => {
      if (configId === null) return createModelConfig(input);
      const { api_key, ...savedFields } = input;
      return updateModelConfig(configId, { ...savedFields, ...(input.provider === "claude_subscription" || api_key ? { api_key } : {}) });
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
    const baseUrl = form.baseUrl.trim();
    const apiKey = form.apiKey.trim();
    setDiscoveredModels(null);
    setDiscoveryHint(null);
    if (form.provider === "claude_subscription" || !baseUrl || !apiKey) return;

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
  }, [form.apiKey, form.baseUrl, form.provider]);

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

  function save() {
    if (!testPassed || saveMutation.isPending) return;
    saveMutation.mutate({ configId: editingId, input: formInput, form });
  }

  function testDraft() {
    if (!formComplete || testMutation.isPending) return;
    testMutation.mutate({ input: formInput, authVersion: claude.authVersion });
  }

  async function remove(config: ModelConfig) {
    await confirmDelete({
      target: `模型配置「${config.config_name}」`,
      consequence: "使用该配置的对话将无法继续选择它。",
      action: () => deleteMutation.mutateAsync(config.config_id),
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
  const saveHint = !connectionReady
    ? "请先登录 Claude，再测试并保存模型配置"
    : testPassed || testMutation.isPending
    ? null
    : formComplete
      ? "请先测试连接，通过后才能保存"
      : "填写全部连接信息并测试连接通过后才能保存";

  return (
    <div className="grid min-w-0 gap-4">
      <div className={cn("space-y-2", variant === "dialog" && "max-h-[min(52vh,28rem)] overflow-y-auto")}>
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
          <div
            key={config.config_id}
            className={cn(
              "border-border/60 flex items-center justify-between rounded-md border p-3",
              editingId === config.config_id && "bg-muted/40",
            )}
          >
            <div className="min-w-0">
              <div className="flex items-center gap-2 text-sm font-medium">
                {config.config_name}
                {config.enabled && (config.provider === "claude_subscription" || config.api_key_configured) ? <CheckCircle2Icon className="text-success size-4" /> : <XCircleIcon className="text-muted-foreground size-4" />}
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
        <label className="grid min-w-0 gap-1 text-xs">连接方式
          <NativeSelect value={form.provider} onChange={(event) => {
            const provider = event.target.value as ModelProvider;
            setForm(changeProvider(form, provider));
          }}>
            <option value="openai">OpenAI 兼容 API</option>
            <option value="claude_subscription">Claude 订阅</option>
          </NativeSelect>
        </label>
        {form.provider === "claude_subscription" && <ClaudeSubscriptionConnection connection={claude} disabled={busy || testMutation.isPending} />}
        <label className="grid min-w-0 gap-1 text-xs">配置名称<Input value={form.configName} onChange={(event) => setForm({ ...form, configName: event.target.value })} placeholder="例如：DeepSeek 官方" /></label>
        {form.provider === "openai" && <>
        <label className="grid min-w-0 gap-1 text-xs">API Base URL<Input value={form.baseUrl} onChange={(event) => setForm({ ...form, baseUrl: event.target.value })} placeholder="https://api.example.com/v1" /></label>
        <label className="grid min-w-0 gap-1 text-xs">API Key<div className="relative min-w-0"><Input type={showApiKey ? "text" : "password"} className="pr-9" value={form.apiKey} onChange={(event) => setForm({ ...form, apiKey: event.target.value })} placeholder="请输入 API Key" /><button type="button" className="text-muted-foreground hover:text-foreground absolute inset-y-0 right-0 inline-flex w-8 items-center justify-center" onClick={() => setShowApiKey((visible) => !visible)} aria-label={showApiKey ? "隐藏 API Key" : "显示 API Key"}>{showApiKey ? <EyeOffIcon className="size-4" /> : <EyeIcon className="size-4" />}</button></div></label>
        </>}
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="grid gap-1 text-xs">
            模型名称
            {form.provider === "claude_subscription" ? (
              <ClaudeSubscriptionModelSelect connection={claude} modelName={form.modelName} onSelect={(model) => setForm((current) => selectClaudeModel(current, model))} />
            ) : discoveredModels ? (
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
    </div>
  );
}
