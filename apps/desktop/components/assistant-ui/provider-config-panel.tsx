"use client";

import { useCallback, useEffect, useState } from "react";
import { CheckCircle2Icon, Loader2Icon, Settings2Icon, Trash2Icon, XCircleIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  createProvider,
  deleteProvider,
  getProviderCatalog,
  getProviders,
  testProvider,
  updateProvider,
  type Provider,
  type ProviderCatalogItem,
} from "@/lib/api/providers";

type FormState = {
  name: string;
  baseUrl: string;
  apiKey: string;
};

const emptyForm: FormState = { name: "deepseek", baseUrl: "", apiKey: "" };

export function ProviderConfigPanel({
  onChanged,
  onBeforeOpen,
}: {
  onChanged: () => void
  onBeforeOpen?: () => void
}) {
  const [open, setOpen] = useState(false);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [catalog, setCatalog] = useState<ProviderCatalogItem[]>([]);
  const [form, setForm] = useState<FormState>(emptyForm);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [testingId, setTestingId] = useState<number | null>(null);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  const loadProviders = useCallback(async () => {
    try {
      const [nextProviders, nextCatalog] = await Promise.all([getProviders(), getProviderCatalog()]);
      setProviders(nextProviders);
      setCatalog(nextCatalog);
      setForm((current) => current.name ? current : { ...current, name: nextCatalog[0]?.name ?? "" });
    } catch {
      setMessage({ ok: false, text: "Provider 列表加载失败，请重试" });
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => void loadProviders(), 0);
    return () => window.clearTimeout(timer);
  }, [loadProviders, open]);

  const resetForm = () => {
    setForm(emptyForm);
    setEditingId(null);
  };

  const save = async () => {
    setBusy(true);
    setMessage(null);
    try {
      if (editingId === null) {
        await createProvider({
          name: form.name.trim().toLowerCase(),
          base_url: form.baseUrl.trim() || undefined,
          api_key: form.apiKey || undefined,
        });
      } else {
        await updateProvider(editingId, {
          base_url: form.baseUrl.trim(),
          ...(form.apiKey ? { api_key: form.apiKey } : {}),
        });
      }
      await loadProviders();
      resetForm();
      onChanged();
      setMessage({ ok: true, text: "Provider 已保存，模型列表已刷新" });
    } catch {
      setMessage({ ok: false, text: "保存失败，请检查配置后重试" });
    } finally {
      setBusy(false);
    }
  };

  const test = async (providerId: number) => {
    setTestingId(providerId);
    setMessage(null);
    try {
      const result = await testProvider(providerId);
      setMessage({
        ok: result.success,
        text: result.success
          ? `连接成功${result.elapsed_ms ? ` · ${result.elapsed_ms}ms` : ""}`
          : result.error_message ?? "连接失败，请检查 Provider 配置",
      });
    } catch {
      setMessage({ ok: false, text: "连接测试失败，请重试" });
    } finally {
      setTestingId(null);
    }
  };

  const remove = async (provider: Provider) => {
    if (!window.confirm(`确定删除 Provider「${provider.name}」吗？`)) return;
    setBusy(true);
    try {
      await deleteProvider(provider.provider_id);
      await loadProviders();
      onChanged();
      setMessage({ ok: true, text: "Provider 已删除，模型列表已刷新" });
    } catch {
      setMessage({ ok: false, text: "删除失败，请重试" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <button
        type="button"
        onClick={() => {
          onBeforeOpen?.()
          setOpen(true)
        }}
        className="text-muted-foreground hover:text-foreground inline-flex h-7 items-center gap-1 rounded-md px-2 text-xs"
      >
        <Settings2Icon className="size-3.5" />
        模型设置
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>模型设置</DialogTitle>
            <DialogDescription>配置模型厂商后，模型会自动出现在选择列表中。</DialogDescription>
          </DialogHeader>

          <div className="max-h-[min(52vh,28rem)] space-y-2 overflow-y-auto">
            {providers.length === 0 && (
              <p className="text-muted-foreground rounded-md border border-dashed p-3 text-sm">暂无已配置 Provider</p>
            )}
            {providers.map((provider) => (
              <div key={provider.provider_id} className="border-border/60 flex items-center justify-between rounded-md border p-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    {provider.name}
                    {provider.enabled && provider.api_key_configured ? <CheckCircle2Icon className="text-emerald-600 size-4" /> : <XCircleIcon className="text-muted-foreground size-4" />}
                  </div>
                  <p className="text-muted-foreground truncate text-xs">{provider.base_url ?? "使用默认地址"}</p>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <Button variant="ghost" size="sm" disabled={testingId !== null} onClick={() => void test(provider.provider_id)}>
                    {testingId === provider.provider_id && <Loader2Icon className="animate-spin" />}
                    测试
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => { setEditingId(provider.provider_id); setForm({ name: provider.name, baseUrl: provider.base_url ?? "", apiKey: "" }); }}>
                    编辑
                  </Button>
                  <Button variant="ghost" size="icon-sm" disabled={busy} onClick={() => void remove(provider)} aria-label={`删除 ${provider.name}`}>
                    <Trash2Icon />
                  </Button>
                </div>
              </div>
            ))}
          </div>

          <div className="border-border/60 space-y-3 rounded-md border p-3">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium">{editingId === null ? "添加 Provider" : "编辑 Provider"}</h3>
              {editingId !== null && <Button variant="ghost" size="sm" onClick={resetForm}>取消编辑</Button>}
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="grid gap-1 text-xs">
                Provider 厂商
                <select className="border-input bg-background h-8 rounded-md border px-2 text-sm" value={form.name} disabled={editingId !== null || catalog.length === 0} onChange={(event) => setForm({ ...form, name: event.target.value })}>
                  {catalog.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
                </select>
              </label>
            </div>
            <label className="grid gap-1 text-xs">
              API Base URL（可选）
              <input className="border-input bg-background h-8 rounded-md border px-2 text-sm" value={form.baseUrl} onChange={(event) => setForm({ ...form, baseUrl: event.target.value })} placeholder="留空使用默认地址" />
            </label>
            <label className="grid gap-1 text-xs">
              API Key {editingId !== null && <span className="text-muted-foreground">（留空表示不修改）</span>}
              <input type="password" className="border-input bg-background h-8 rounded-md border px-2 text-sm" value={form.apiKey} onChange={(event) => setForm({ ...form, apiKey: event.target.value })} placeholder="不会回显已保存 Key" />
            </label>
            <Button className="w-full" disabled={busy || catalog.length === 0 || !form.name || (editingId === null && !form.apiKey)} onClick={() => void save()}>
              {busy && <Loader2Icon className="animate-spin" />}
              保存配置
            </Button>
          </div>

          {message && <p className={message.ok ? "text-emerald-600 text-sm" : "text-destructive text-sm"}>{message.text}</p>}
        </DialogContent>
      </Dialog>
    </>
  );
}
