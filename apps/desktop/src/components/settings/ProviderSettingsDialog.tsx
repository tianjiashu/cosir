/**
 * 模型厂商配置中心主对话框（设计 §10.2）。
 *
 * 编排厂商列表与其全部变更链路：
 * - 厂商卡片：启用 Switch（即时 PUT 生效）、模型数量、base_url、Key 配置徽标
 *   （未配置显告警色）、编辑（嵌套 ProviderFormDialog）、删除（按钮级二次确认）、
 *   展开的模型管理区（ProviderModelsSection）；
 * - 新增厂商（嵌套 ProviderFormDialog 空表单）；
 * - 任一变更（启停/保存/删除）成功后刷新厂商列表与 taskStore 可用模型缓存（设计 §9.5）。
 *
 * @module components/settings/ProviderSettingsDialog
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Loader2, Pencil, Plus, Trash2 } from "lucide-react";
import type { ProviderRecord } from "@shared/model";
import { cn } from "@/lib/utils";
import { logError, logInfo } from "@/lib/logger";
import {
  createProvider,
  deleteProvider,
  listProviders,
  updateProvider,
} from "@/services/api";
import { useTaskStore } from "@/stores/taskStore";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Caption, Panel } from "@/components/ui/tokens";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import {
  ProviderFormDialog,
  type ProviderFormMeta,
  type ProviderFormValues,
} from "./ProviderFormDialog";
import { ProviderModelsSection } from "./ProviderModelsSection";

/** ProviderSettingsDialog 组件属性。 */
interface ProviderSettingsDialogProps {
  /** 是否打开（受控）。 */
  open: boolean;
  /** 打开状态变更回调。 */
  onOpenChange: (open: boolean) => void;
}

/**
 * 厂商配置中心对话框。
 *
 * 打开时拉取厂商列表；所有变更（启停/编辑/删除）成功后就地刷新
 * 列表并同步 taskStore.refreshAvailableModels，保证发送前校验与下拉即时
 * 反映配置变更。
 */
export function ProviderSettingsDialog({ open, onOpenChange }: ProviderSettingsDialogProps) {
  const [providers, setProviders] = useState<ProviderRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** 嵌套表单（新增/编辑）状态：null 关闭；{mode, provider} 打开。 */
  const [formState, setFormState] = useState<{
    mode: "create" | "edit";
    provider: ProviderRecord | null;
  } | null>(null);
  /** 删除二次确认中的厂商 ID（按钮级确认，避免再开一层 Dialog）。 */
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);
  /** 展开模型管理区的厂商 ID（单展开，折叠互斥）。 */
  const [expandedId, setExpandedId] = useState<string | null>(null);
  /** 删除确认超时句柄（超时自动回退，防止误停留确认态）。 */
  const confirmTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refreshProviders = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const list = await listProviders();
      setProviders(list);
    } catch (err) {
      logError("拉取厂商列表失败", err, { module: "ProviderSettingsDialog" });
      setError("拉取厂商列表失败，请检查后端是否可用");
    } finally {
      setLoading(false);
    }
  }, []);

  // 打开时拉取一次列表；关闭时清理确认与展开态。
  useEffect(() => {
    if (open) {
      void refreshProviders();
    } else {
      setConfirmingDeleteId(null);
      setExpandedId(null);
      setFormState(null);
    }
  }, [open, refreshProviders]);

  // 卸载清理确认超时句柄。
  useEffect(() => {
    return () => {
      if (confirmTimeoutRef.current) {
        clearTimeout(confirmTimeoutRef.current);
      }
    };
  }, []);

  /**
   * 变更成功后的统一刷新：厂商列表 + 可用模型缓存（§9.5）。
   */
  const refreshAll = useCallback(async () => {
    await Promise.all([refreshProviders(), useTaskStore.getState().refreshAvailableModels()]);
  }, [refreshProviders]);

  /** 启停厂商（Switch 即时生效；失败回滚 UI 由重新拉取保证）。 */
  const handleToggleEnabled = async (provider: ProviderRecord, enabled: boolean) => {
    try {
      const updated = await updateProvider(provider.provider_id, { enabled });
      setProviders((prev) =>
        prev.map((item) => (item.provider_id === updated.provider_id ? updated : item)),
      );
      void useTaskStore.getState().refreshAvailableModels();
    } catch (err) {
      logError("切换厂商启用状态失败", err, {
        module: "ProviderSettingsDialog",
        provider_id: provider.provider_id,
      });
      setError("切换失败，请重试");
      void refreshProviders();
    }
  };

  /** 删除厂商（级联删模型，幂等）；按钮级二次确认防误删。 */
  const handleDelete = async (provider: ProviderRecord) => {
    if (confirmingDeleteId !== provider.provider_id) {
      // 第一次点击：进入确认态，5 秒未确认自动回退。
      setConfirmingDeleteId(provider.provider_id);
      if (confirmTimeoutRef.current) {
        clearTimeout(confirmTimeoutRef.current);
      }
      confirmTimeoutRef.current = setTimeout(() => setConfirmingDeleteId(null), 5000);
      return;
    }
    if (confirmTimeoutRef.current) {
      clearTimeout(confirmTimeoutRef.current);
    }
    setConfirmingDeleteId(null);
    try {
      await deleteProvider(provider.provider_id);
      logInfo("删除模型厂商", {
        module: "ProviderSettingsDialog",
        provider_id: provider.provider_id,
      });
      if (expandedId === provider.provider_id) {
        setExpandedId(null);
      }
      await refreshAll();
    } catch (err) {
      logError("删除厂商失败", err, {
        module: "ProviderSettingsDialog",
        provider_id: provider.provider_id,
      });
      setError("删除失败，请重试");
    }
  };

  /**
   * 提交厂商表单：create/edit 双模式。
   *
   * api_key 归一化语义（与后端 update 契约一致）：
   * - 创建：空串 → null（不设置）；非空 → 明文写入；
   * - 编辑：非空 → 更新；空串 + 显式清除标记 → ""（清空）；空串未标记 → 不更新。
   * base_url 编辑直接透传（后端 "" = 置空回落 litellm 内置解析）。
   *
   * 错误处理：本函数不在内部吞掉异常——失败时直接向上抛出真实的
   * ``ServiceError``（含 ``message`` / ``statusCode``），交由嵌套的
   * ``ProviderFormDialog.handleSubmit`` 统一展示后端返回的真实错误，避免
   * 出现「保存失败，请检查名称是否重复或网络是否可用」这类与根因无关的
   * 写死兜底文案（历史上曾因 SQLite 缺列导致 500，却被误导为名称重复）。
   *
   * @param values - 表单值。
   * @param meta - 提交元信息（clearKey 编辑模式显式清空标记）。
   * @throws ServiceError - 后端返回的业务/系统错误（含真实 message 与 statusCode）。
   * @returns 成功时无返回值（失败不返回，直接抛异常）。
   */
  const handleFormSubmit = async (
    values: ProviderFormValues,
    meta: ProviderFormMeta,
  ): Promise<void> => {
    if (!formState) throw new Error("表单未处于打开状态");
    const isCreate = formState.mode === "create";
    if (isCreate) {
      await createProvider({
        name: values.name,
        type: values.type,
        base_url: values.baseUrl === "" ? null : values.baseUrl,
        api_key: values.apiKey === "" ? null : values.apiKey,
        enabled: true,
      });
    } else if (formState.provider) {
      await updateProvider(formState.provider.provider_id, {
        name: values.name,
        type: values.type,
        // 后端语义："" = 显式置空，undefined = 不更新。
        base_url: values.baseUrl,
        api_key: values.apiKey !== "" ? values.apiKey : meta.clearKey ? "" : undefined,
      });
    } else {
      throw new Error("编辑模式未指定厂商");
    }
    setFormState(null);
    await refreshAll();
  };

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-2xl">
          <DialogTitle>模型厂商配置</DialogTitle>

          <div className="flex items-center justify-between">
            <p className="text-xs text-muted-foreground">
              {providers.length > 0 ? `${providers.length} 个厂商` : ""}
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setFormState({ mode: "create", provider: null })}
            >
              <Plus className="mr-1 h-3.5 w-3.5" />
              新增厂商
            </Button>
          </div>

          {loading && providers.length === 0 ? (
            <div className="flex items-center justify-center py-8 text-xs text-muted-foreground">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              加载厂商列表...
            </div>
          ) : providers.length === 0 ? (
            /* 空态：尚未配置任何厂商，提示用户通过「新增厂商」入口添加。 */
            <div className="flex flex-col items-center gap-3 rounded border border-dashed border-border py-8">
              <p className="text-sm text-muted-foreground">尚未配置任何厂商</p>
            </div>
          ) : (
            <div className={`${Panel.codeBlockMaxHeight} space-y-2 overflow-y-auto pr-1`}>
              {providers.map((provider) => {
                const expanded = expandedId === provider.provider_id;
                const confirming = confirmingDeleteId === provider.provider_id;
                return (
                  <div key={provider.provider_id} className="rounded border border-border p-3">
                    {/* 卡片头：启停 + 名称/类型 + Key 徽标 + 操作 */}
                    <div className="flex items-center gap-2">
                      <Switch
                        checked={provider.enabled}
                        onCheckedChange={(checked) => void handleToggleEnabled(provider, checked)}
                        aria-label={`启用 ${provider.name}`}
                      />
                      <button
                        type="button"
                        className="flex min-w-0 flex-1 cursor-pointer items-center gap-2 text-left"
                        onClick={() => setExpandedId(expanded ? null : provider.provider_id)}
                      >
                        {expanded ? (
                          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                        ) : (
                          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                        )}
                        <span className="truncate text-sm font-medium">{provider.name}</span>
                        <Badge
                          variant="secondary"
                          className={`shrink-0 ${Caption.xs10}`}
                        >
                          {provider.type}
                        </Badge>
                        <span className="shrink-0 text-xs text-muted-foreground">
                          {provider.model_count} 模型
                        </span>
                        {/* Key 未配置徽标：告警色提示（§10.2） */}
                        {!provider.api_key_configured && (
                          <span className={`shrink-0 ${Caption.xs10} text-amber-500`}>
                            Key 未设置
                          </span>
                        )}
                      </button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        className="h-6 w-6 shrink-0"
                        aria-label={`编辑 ${provider.name}`}
                        onClick={() => setFormState({ mode: "edit", provider })}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        className={cn(
                          "h-6 w-6 shrink-0",
                          confirming ? "text-destructive" : "text-muted-foreground",
                        )}
                        aria-label={`删除 ${provider.name}`}
                        onClick={() => void handleDelete(provider)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>

                    {/* 元信息行 */}
                    <div
                      className={`mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 pl-6 ${Caption.xs} text-muted-foreground`}
                    >
                      <span>base_url: {provider.base_url || "内置解析"}</span>
                      <span>Key: {provider.api_key_configured ? "已设置" : "未设置"}</span>
                      {confirming && (
                        <span className="text-destructive">
                          再点一次删除按钮确认（级联删除 {provider.model_count} 个模型）
                        </span>
                      )}
                    </div>

                    {/* 展开的模型管理区 */}
                    {expanded && (
                      <div className="mt-2">
                        <ProviderModelsSection
                          providerId={provider.provider_id}
                          typePrefix={provider.type === "custom" ? "custom" : provider.type}
                          onImported={() => void refreshAll()}
                        />
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {error && (
            <p className="text-xs text-destructive" role="alert">
              {error}
            </p>
          )}
        </DialogContent>
      </Dialog>

      {/* 嵌套的新增/编辑表单（Radix Dialog 叠加） */}
      <ProviderFormDialog
        open={formState !== null}
        onOpenChange={(next) => {
          if (!next) setFormState(null);
        }}
        provider={formState?.mode === "edit" ? formState.provider : null}
        onSubmit={handleFormSubmit}
      />
    </>
  );
}
