/**
 * 厂商卡片内的模型条目管理区（配置中心子组件，设计 §10.2）。
 *
 * 两条导入通道（统一走 POST /providers/{id}/models 批量导入）：
 * 1. 「从目录发现」：POST /discover 拉 litellm 候选列表 → 勾选（已导入项
 *    already_imported 置灰）→ 批量导入，窗口/thinking 由 litellm 目录预填；
 * 2. 手动添加行：model_name（完整路由名）/display_name/窗口/thinking 单条导入。
 *
 * @module components/settings/ProviderModelsSection
 */

import { useState } from "react";
import { Loader2, Plus, Search } from "lucide-react";
import type { ModelCandidate } from "@shared/model";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Caption } from "@/components/ui/tokens";
import { discoverProviderModels, importProviderModels } from "@/services/api";
import { logError } from "@/lib/logger";

/** ProviderModelsSection 组件属性。 */
interface ProviderModelsSectionProps {
  /** 归属厂商标识。 */
  providerId: string;
  /** 厂商类型的 litellm 路由前缀（手动添加行的 model_name 提示用）。 */
  typePrefix: string;
  /**
   * 任一导入成功后的回调（父组件刷新厂商列表与可用模型缓存，设计 §9.5）。
   */
  onImported: () => void;
}

/** 手动添加行的本地表单状态。 */
interface ManualModelDraft {
  modelName: string;
  displayName: string;
  contextWindow: string;
  supportsThinking: boolean;
}

/** 手动添加行初始态（窗口默认 128k，主流模型安全下限）。 */
const EMPTY_DRAFT: ManualModelDraft = {
  modelName: "",
  displayName: "",
  contextWindow: "128000",
  supportsThinking: false,
};

/**
 * 模型条目管理区（厂商卡片展开内容）。
 *
 * discover 与手动添加互不阻塞；导入结果（成功数/跳过重名）就地反馈，
 * 错误经 ServiceError message 内联展示并写日志。
 */
export function ProviderModelsSection({
  providerId,
  typePrefix,
  onImported,
}: ProviderModelsSectionProps) {
  const [discovering, setDiscovering] = useState(false);
  const [candidates, setCandidates] = useState<ModelCandidate[] | null>(null);
  const [selectedNames, setSelectedNames] = useState<Set<string>>(new Set());
  const [importing, setImporting] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<ManualModelDraft>(EMPTY_DRAFT);
  /** 手动添加行内的候选过滤词（长列表检索）。 */
  const [candidateFilter, setCandidateFilter] = useState("");

  /** 拉取 litellm 目录候选列表（外部依赖失败转 502 语义，就地展示错误）。 */
  const handleDiscover = async () => {
    setDiscovering(true);
    setError(null);
    setFeedback(null);
    try {
      const result = await discoverProviderModels(providerId);
      setCandidates(result);
      setSelectedNames(new Set());
      if (result.length === 0) {
        setFeedback("目录中没有该类型的候选模型，可手动添加");
      }
    } catch (err) {
      logError("发现模型失败", err, { module: "ProviderModelsSection", provider_id: providerId });
      setError("发现模型失败（litellm 目录不可用或超时），请稍后重试或手动添加");
    } finally {
      setDiscovering(false);
    }
  };

  /** 勾选/取消勾选候选模型。 */
  const toggleCandidate = (modelName: string) => {
    setSelectedNames((prev) => {
      const next = new Set(prev);
      if (next.has(modelName)) {
        next.delete(modelName);
      } else {
        next.add(modelName);
      }
      return next;
    });
  };

  /** 批量导入勾选的候选（预填 litellm 窗口/thinking；重名由后端幂等跳过）。 */
  const handleImportSelected = async () => {
    if (selectedNames.size === 0 || !candidates) return;
    setImporting(true);
    setError(null);
    try {
      const picked = candidates.filter(
        (item) => selectedNames.has(item.model_name) && !item.already_imported,
      );
      if (picked.length === 0) {
        setFeedback("勾选的条目均已导入");
        return;
      }
      const result = await importProviderModels(
        providerId,
        picked.map((item) => ({
          model_name: item.model_name,
          display_name: item.display_name,
          max_context_window: item.max_context_window,
          supports_thinking: item.supports_thinking,
        })),
      );
      const skipped = result.skipped_model_names.length;
      setFeedback(
        `已导入 ${result.imported.length} 个模型${skipped > 0 ? `，跳过重名 ${skipped} 个` : ""}`,
      );
      setSelectedNames(new Set());
      // 标记已导入态（重新 discover 也可，但本地标记免一次外部调用）。
      const importedNames = new Set(result.imported.map((item) => item.model_name));
      setCandidates(
        (candidates ?? []).map((item) =>
          importedNames.has(item.model_name) ? { ...item, already_imported: true } : item,
        ),
      );
      onImported();
    } catch (err) {
      logError("批量导入模型失败", err, { module: "ProviderModelsSection", provider_id: providerId });
      setError("导入失败，请检查网络或稍后重试");
    } finally {
      setImporting(false);
    }
  };

  /** 手动添加单条模型（display_name 留空时用路由名去前缀；窗口非法回退 128k）。 */
  const handleAddManual = async () => {
    const modelName = draft.modelName.trim();
    if (!modelName) {
      setError("请填写 model_name（完整路由名）");
      return;
    }
    setImporting(true);
    setError(null);
    try {
      // display_name 优先用户输入；留空时取路由名去前缀（无前缀则原样）。
      const trimmedDisplay = draft.displayName.trim();
      const displayName =
        trimmedDisplay !== ""
          ? trimmedDisplay
          : modelName.includes("/")
            ? modelName.split("/").slice(1).join("/") || modelName
            : modelName;
      const contextWindow = Number.parseInt(draft.contextWindow, 10);
      await importProviderModels(providerId, [
        {
          model_name: modelName,
          display_name: displayName,
          max_context_window: Number.isFinite(contextWindow) && contextWindow > 0
            ? contextWindow
            : 128000,
          supports_thinking: draft.supportsThinking,
        },
      ]);
      setFeedback(`已添加模型 ${modelName}`);
      setDraft(EMPTY_DRAFT);
      onImported();
    } catch (err) {
      logError("手动添加模型失败", err, { module: "ProviderModelsSection", provider_id: providerId });
      setError("添加失败：model_name 可能与既有条目重复或格式非法");
    } finally {
      setImporting(false);
    }
  };

  /** 过滤后的候选列表（按 model_name/display_name 模糊匹配）。 */
  const filteredCandidates = (candidates ?? []).filter((item) => {
    if (!candidateFilter.trim()) return true;
    const needle = candidateFilter.trim().toLowerCase();
    return (
      item.model_name.toLowerCase().includes(needle) ||
      item.display_name.toLowerCase().includes(needle)
    );
  });

  return (
    <div className="space-y-3 border-t border-border pt-3">
      {/* 通道 1：目录发现 */}
      <div className="flex items-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => void handleDiscover()}
          disabled={discovering}
        >
          {discovering ? (
            <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
          ) : (
            <Search className="mr-1 h-3.5 w-3.5" />
          )}
          从目录发现模型
        </Button>
        {candidates !== null && selectedNames.size > 0 && (
          <Button
            type="button"
            variant="primary"
            size="sm"
            onClick={() => void handleImportSelected()}
            disabled={importing}
          >
            {importing && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
            导入所选（{selectedNames.size}）
          </Button>
        )}
      </div>

      {candidates !== null && (
        <div className="space-y-1.5">
          <Input
            value={candidateFilter}
            onChange={(e) => setCandidateFilter(e.target.value)}
            placeholder="过滤候选（名称匹配）..."
            className="h-7 text-xs"
          />
          <div className="max-h-44 space-y-0.5 overflow-y-auto rounded border border-border p-1">
            {filteredCandidates.length === 0 ? (
              <p className="px-2 py-1.5 text-xs text-muted-foreground">无匹配候选</p>
            ) : (
              filteredCandidates.map((candidate) => (
                <label
                  key={candidate.model_name}
                  className={`flex items-center gap-2 rounded px-1.5 py-1 text-xs ${
                    candidate.already_imported
                      ? "opacity-50"
                      : "cursor-pointer hover:bg-accent transition-colors"
                  }`}
                >
                  <Checkbox
                    checked={selectedNames.has(candidate.model_name)}
                    disabled={candidate.already_imported || importing}
                    onCheckedChange={() => toggleCandidate(candidate.model_name)}
                  />
                  <span className="flex-1 truncate">{candidate.display_name}</span>
                  <span className={`shrink-0 ${Caption.xs10} text-muted-foreground`}>
                    {candidate.max_context_window > 0
                      ? `${Math.round(candidate.max_context_window / 1000)}k`
                      : "?"}
                  </span>
                  {candidate.supports_thinking && (
                    <span className={`shrink-0 ${Caption.xs10} text-amber-500`}>thinking</span>
                  )}
                  {candidate.already_imported && (
                    <span className={`shrink-0 ${Caption.xs10} text-muted-foreground`}>
                      已导入
                    </span>
                  )}
                </label>
              ))
            )}
          </div>
        </div>
      )}

      {/* 通道 2：手动添加 */}
      <div className="space-y-1.5 rounded border border-border p-2">
        <p className="text-xs font-medium">手动添加模型</p>
        <div className="flex items-center gap-1.5">
          <Input
            value={draft.modelName}
            onChange={(e) => setDraft((prev) => ({ ...prev, modelName: e.target.value }))}
            placeholder={`完整路由名，如 ${typePrefix}/model-id`}
            className="h-7 flex-1 text-xs"
          />
          <Input
            value={draft.displayName}
            onChange={(e) => setDraft((prev) => ({ ...prev, displayName: e.target.value }))}
            placeholder="显示名（可选）"
            className="h-7 w-32 text-xs"
          />
          <Input
            value={draft.contextWindow}
            onChange={(e) => setDraft((prev) => ({ ...prev, contextWindow: e.target.value }))}
            placeholder="窗口"
            className="h-7 w-20 text-xs"
          />
          <label className="flex shrink-0 cursor-pointer items-center gap-1 text-xs text-muted-foreground">
            <Checkbox
              checked={draft.supportsThinking}
              onCheckedChange={(checked) =>
                setDraft((prev) => ({ ...prev, supportsThinking: checked === true }))
              }
            />
            thinking
          </label>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-7 shrink-0"
            onClick={() => void handleAddManual()}
            disabled={importing}
          >
            {importing ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Plus className="h-3.5 w-3.5" />
            )}
          </Button>
        </div>
      </div>

      {feedback && <p className="text-xs text-emerald-500">{feedback}</p>}
      {error && (
        <p className="text-xs text-destructive" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
