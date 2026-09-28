"use client";

import { useCallback, useEffect, useState } from "react";
import { CheckCircle2Icon, EyeIcon, EyeOffIcon, Loader2Icon, Settings2Icon, Trash2Icon, XCircleIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
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
  getModelConfigs,
  testDraftModelConfig,
  testModelConfig,
  updateModelConfig,
  type ModelConfig,
} from "@/lib/api/model-configs";
import { HttpError } from "@/lib/http/errors";

type FormState = {
  configName: string;
  baseUrl: string;
  apiKey: string;
  modelName: string;
  contextWindowK: string;
};

const emptyForm: FormState = {
  configName: "",
  baseUrl: "",
  apiKey: "",
  modelName: "",
  contextWindowK: "128",
};

export function ModelConfigPanel({
  onChanged,
  onBeforeOpen,
}: {
  onChanged: () => void;
  onBeforeOpen?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [configs, setConfigs] = useState<ModelConfig[]>([]);
  const [form, setForm] = useState<FormState>(emptyForm);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [showApiKey, setShowApiKey] = useState(false);
  const [busy, setBusy] = useState(false);
  const [testingId, setTestingId] = useState<number | null>(null);
  const [testingDraft, setTestingDraft] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  const loadConfigs = useCallback(async () => {
    try {
      setConfigs(await getModelConfigs());
    } catch {
      setMessage({ ok: false, text: "模型配置加载失败，请重试" });
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => void loadConfigs(), 0);
    return () => window.clearTimeout(timer);
  }, [loadConfigs, open]);

  const resetForm = () => {
    setForm(emptyForm);
    setEditingId(null);
    setShowApiKey(false);
  };

  const save = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const input = {
        config_name: form.configName.trim(),
        base_url: form.baseUrl.trim(),
        api_key: form.apiKey,
        model_name: form.modelName.trim(),
        context_window_k: Number(form.contextWindowK),
      };
      let savedConfig: ModelConfig;
      if (editingId === null) {
        savedConfig = await createModelConfig(input);
      } else {
        const { api_key, ...savedFields } = input;
        savedConfig = await updateModelConfig(editingId, {
          ...savedFields,
          ...(api_key ? { api_key } : {}),
        });
      }
      await loadConfigs();
      if (editingId === null) setEditingId(savedConfig.config_id);
      setShowApiKey(false);
      setMessage({ ok: true, text: "模型配置已保存" });
    } catch {
      setMessage({ ok: false, text: "保存失败，请检查配置后重试" });
    } finally {
      setBusy(false);
    }
  };

  const testDraft = async () => {
    setTestingDraft(true);
    setMessage(null);
    try {
      const result = await testDraftModelConfig({
        base_url: form.baseUrl.trim(),
        api_key: form.apiKey,
        model_name: form.modelName.trim(),
      });
      setMessage({
        ok: result.success,
        text: result.success
          ? `连接成功${result.elapsed_ms ? ` · ${result.elapsed_ms}ms` : ""}`
          : result.error_message ?? "连接失败，请检查配置",
      });
    } catch {
      setMessage({ ok: false, text: "连接测试失败，请重试" });
    } finally {
      setTestingDraft(false);
    }
  };

  const testSaved = async (configId: number) => {
    setTestingId(configId);
    setMessage(null);
    try {
      const result = await testModelConfig(configId);
      setMessage({
        ok: result.success,
        text: result.success
          ? `连接成功${result.elapsed_ms ? ` · ${result.elapsed_ms}ms` : ""}`
          : result.error_message ?? "连接失败，请检查模型配置",
      });
    } catch {
      setMessage({ ok: false, text: "连接测试失败，请重试" });
    } finally {
      setTestingId(null);
    }
  };

  const remove = async (config: ModelConfig) => {
    if (!window.confirm(`确定删除模型配置「${config.config_name}」吗？`)) return;
    setBusy(true);
    try {
      await deleteModelConfig(config.config_id);
      await loadConfigs();
      setMessage({ ok: true, text: "模型配置已删除" });
    } catch (cause) {
      setMessage({
        ok: false,
        text: cause instanceof HttpError && cause.message ? cause.message : "删除失败，请重试",
      });
    } finally {
      setBusy(false);
    }
  };

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
          setOpen(nextOpen);
          if (!nextOpen) onChanged();
        }}
      >
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>模型设置</DialogTitle>
            <DialogDescription>填写 OpenAI 兼容接口的连接配置，模型会出现在选择列表中。</DialogDescription>
          </DialogHeader>

          <div className="max-h-[min(52vh,28rem)] space-y-2 overflow-y-auto">
            {configs.length === 0 && (
              <p className="text-muted-foreground rounded-md border border-dashed p-3 text-sm">暂无已配置模型</p>
            )}
            {configs.map((config) => (
              <div key={config.config_id} className="border-border/60 flex items-center justify-between rounded-md border p-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    {config.config_name}
                    {config.enabled && config.api_key_configured ? <CheckCircle2Icon className="text-emerald-600 size-4" /> : <XCircleIcon className="text-muted-foreground size-4" />}
                  </div>
                  <p className="text-muted-foreground truncate text-xs">{config.model_name} · {config.context_window_k}K · {config.base_url}</p>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <Button variant="ghost" size="sm" disabled={testingId !== null} onClick={() => void testSaved(config.config_id)}>
                    {testingId === config.config_id && <Loader2Icon className="animate-spin" />}
                    测试
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => { setEditingId(config.config_id); setForm({ configName: config.config_name, baseUrl: config.base_url, apiKey: config.api_key, modelName: config.model_name, contextWindowK: String(config.context_window_k) }); setShowApiKey(false); }}>
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
            <label className="grid gap-1 text-xs">配置名称<input className="border-input bg-background h-8 rounded-md border px-2 text-sm" value={form.configName} onChange={(event) => setForm({ ...form, configName: event.target.value })} placeholder="例如：DeepSeek 官方" /></label>
            <label className="grid gap-1 text-xs">API Base URL<input className="border-input bg-background h-8 rounded-md border px-2 text-sm" value={form.baseUrl} onChange={(event) => setForm({ ...form, baseUrl: event.target.value })} placeholder="https://api.example.com/v1" /></label>
            <label className="grid gap-1 text-xs">API Key<div className="relative"><input type={showApiKey ? "text" : "password"} className="border-input bg-background h-8 w-full rounded-md border px-2 pr-9 text-sm" value={form.apiKey} onChange={(event) => setForm({ ...form, apiKey: event.target.value })} placeholder="请输入 API Key" /><button type="button" className="text-muted-foreground hover:text-foreground absolute inset-y-0 right-0 inline-flex w-8 items-center justify-center" onClick={() => setShowApiKey((visible) => !visible)} aria-label={showApiKey ? "隐藏 API Key" : "显示 API Key"}>{showApiKey ? <EyeOffIcon className="size-4" /> : <EyeIcon className="size-4" />}</button></div></label>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="grid gap-1 text-xs">模型名称<input className="border-input bg-background h-8 rounded-md border px-2 text-sm" value={form.modelName} onChange={(event) => setForm({ ...form, modelName: event.target.value })} placeholder="例如：deepseek-chat" /></label>
              <label className="grid gap-1 text-xs">上下文窗口（K）<input type="number" min="1" step="1" className="border-input bg-background h-8 rounded-md border px-2 text-sm" value={form.contextWindowK} onChange={(event) => setForm({ ...form, contextWindowK: event.target.value })} placeholder="例如：128" /></label>
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              <Button variant="outline" disabled={busy || testingDraft || !form.baseUrl.trim() || !form.apiKey || !form.modelName.trim()} onClick={() => void testDraft()}>{testingDraft && <Loader2Icon className="animate-spin" />}测试连接</Button>
              <Button disabled={busy || !form.configName.trim() || !form.baseUrl.trim() || (editingId === null && !form.apiKey) || !form.modelName.trim() || !Number.isInteger(Number(form.contextWindowK)) || Number(form.contextWindowK) <= 0} onClick={() => void save()}>{busy && <Loader2Icon className="animate-spin" />}保存配置</Button>
            </div>
          </div>

          {message && <p className={message.ok ? "text-emerald-600 text-sm" : "text-destructive text-sm"}>{message.text}</p>}
        </DialogContent>
      </Dialog>
    </>
  );
}
