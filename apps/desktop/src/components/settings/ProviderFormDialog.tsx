/**
 * 模型厂商新增/编辑表单对话框（配置中心子组件，设计 §10.2）。
 *
 * react-hook-form + zod 校验：名称（必填）、类型（15 预设，与后端注册表对齐）、
 * base_url（选填，空则用 litellm 内置解析）、api_key（选填明文，password 输入框；
 * Key 明文保存于后端 DB providers.api_key，DB 为唯一事实来源，响应与日志不回传明文）。
 *
 * 编辑模式展示既有厂商的 Key 配置状态（api_key_configured，聚合自后端），
 * 不回显明文；输入为空时提交不更新 Key，可显式点击「清除」标记提交时清空。
 * 编辑模式额外提供「测试连接」按钮（调 POST /providers/{id}/test，设计文档
 * §三 用户视角三件套），用已配置参数发起一次最小 chat 请求验证凭据 / 端点可用性。
 *
 * 厂商类型对应的提示文案：
 * - qianfan / xfyun：提示「litellm 1.97.0 已废弃原生前缀，走 openai/ 兼容组，
 *   请填官方 base_url」；
 * - moonshot / minimax / zai / dashscope：提示国内 / 国际端点分区域，国内需
 *   在 base_url 切换区域端点；
 * - azure：提示「在 Azure Portal 创建 deployment 后手填 deployment 名作为模型名」。
 *
 * @module components/settings/ProviderFormDialog
 */

import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Check, Loader2, X } from "lucide-react";
import type { ProviderRecord, ProviderType } from "@shared/model";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { testProviderConnection } from "@/services/api";
import { ServiceError } from "@/services/types";
import { logError } from "@/lib/logger";

/**
 * 厂商类型预设（与后端 ``PROVIDER_CAPABILITIES`` 注册表 15 类对齐）。
 *
 * label 用「品牌名（说明）」格式，让用户一眼可识别；value 与后端注册表键一致。
 */
const PROVIDER_TYPE_OPTIONS: { value: ProviderType; label: string }[] = [
  { value: "deepseek", label: "DeepSeek（官方）" },
  { value: "openai-compatible", label: "OpenAI 兼容接口" },
  { value: "anthropic", label: "Anthropic" },
  { value: "gemini", label: "Google Gemini" },
  { value: "azure", label: "Azure OpenAI" },
  { value: "dashscope", label: "通义千问（DashScope）" },
  { value: "moonshot", label: "Kimi（月之暗面）" },
  { value: "zai", label: "智谱 GLM" },
  { value: "volcengine", label: "火山方舟 / 豆包" },
  { value: "tencent", label: "腾讯混元" },
  { value: "minimax", label: "MiniMax" },
  { value: "ollama", label: "Ollama（本地）" },
  { value: "qianfan", label: "百度文心一言" },
  { value: "xfyun", label: "讯飞星火" },
  { value: "custom", label: "自定义" },
];

/**
 * 厂商表单校验 schema（zod v4）。
 *
 * - ``base_url`` 允许空 = 用 litellm 内置解析；非空时校验为有效 http(s)。
 */
const providerFormSchema = z.object({
  name: z.string().min(1, "请输入厂商名称"),
  type: z.enum([
    "deepseek",
    "openai-compatible",
    "anthropic",
    "gemini",
    "azure",
    "dashscope",
    "moonshot",
    "zai",
    "volcengine",
    "tencent",
    "minimax",
    "ollama",
    "qianfan",
    "xfyun",
    "custom",
  ]),
  baseUrl: z
    .string()
    .trim()
    .refine((value) => value === "" || URL.canParse(value), "请输入有效的 http(s) 地址"),
  apiKey: z.string(),
});

/** 厂商表单值类型（zod 推断；空串语义由调用方按创建/编辑归一化）。 */
export type ProviderFormValues = z.infer<typeof providerFormSchema>;

/** 提交元信息（表单值之外的提交态）。 */
export interface ProviderFormMeta {
  /** 编辑模式显式标记「清除已设置 Key」（仅编辑且输入为空时可触发）。 */
  clearKey: boolean;
}

/** 测试连接结果展示态（null = 未测试 / pending / done-with-result）。 */
interface ConnectionTestState {
  /** 测试是否进行中（pending 时禁用按钮）。 */
  pending: boolean;
  /** 测试结果（success + 可读消息）；null = 未测试或 pending。 */
  result: { success: boolean; message: string } | null;
}

/** ProviderFormDialog 组件属性。 */
interface ProviderFormDialogProps {
  /** 是否打开（受控）。 */
  open: boolean;
  /** 打开状态变更回调。 */
  onOpenChange: (open: boolean) => void;
  /** 待编辑的厂商；null = 新增模式。 */
  provider: ProviderRecord | null;
  /**
   * 提交回调（创建或更新由调用方根据 provider 是否为 null 区分）。
   *
   * @param values - 归一化后的表单值（空串已转 null）。
   * @param meta - 提交元信息（是否标记清除 Key）。
   * @returns 提交成功无返回值；失败以抛出的 ServiceError 表达（调用方不在内部吞错）。
   */
  onSubmit: (values: ProviderFormValues, meta: ProviderFormMeta) => Promise<void>;
}

/**
 * 返回厂商类型对应的区域 / 端点提示文案（设计文档 §三）。
 *
 * 用于 base_url 字段下方展示：litellm 已废弃原生前缀的厂商（qianfan / xfyun）
 * 提示走 openai/ 兼容组；国内 / 国际分区域的厂商提示在 base_url 切换区域端点；
 * Azure 提示需在 Portal 创建 deployment 后手填 deployment 名作为模型名。
 *
 * @param type - 当前选择的厂商类型。
 * @returns 提示文案；空字符串表示该类型无需额外提示。
 */
function getProviderHint(type: ProviderType): string {
  switch (type) {
    case "qianfan":
      return "litellm 1.97.0 已废弃 qianfan 前缀，走 openai/ 兼容组，请填官方 base_url";
    case "xfyun":
      return "litellm 1.97.0 无 xfyun 前缀，走 openai/ 兼容组，请填官方 base_url";
    case "moonshot":
    case "minimax":
    case "zai":
    case "dashscope":
      return "国内 / 国际端点分区域，国内请在 base_url 切换区域端点";
    case "azure":
      return "Azure OpenAI：需在 Portal 创建 deployment 后手填 deployment 名作为模型名";
    default:
      return "";
  }
}

/**
 * 厂商新增/编辑表单对话框。
 *
 * 编辑模式下用既有厂商预填表单（apiKey 始终为空，不回显明文）；提交成功后
 * 由父组件关闭并刷新列表。Key 状态徽标与「清除」按钮仅编辑模式展示。
 * 编辑模式额外提供「测试连接」按钮，调 POST /providers/{id}/test 验证凭据
 * 与端点可用性，结果就地展示成功 / 失败 + 可读消息。
 */
export function ProviderFormDialog({
  open,
  onOpenChange,
  provider,
  onSubmit,
}: ProviderFormDialogProps) {
  const isEdit = provider !== null;
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** 编辑模式是否已标记「清除已设置 Key」（再次点击可取消）。 */
  const [clearKey, setClearKey] = useState(false);
  /** 测试连接结果展示态（仅编辑模式可见，新增模式无 provider_id 不可测）。 */
  const [testState, setTestState] = useState<ConnectionTestState>({
    pending: false,
    result: null,
  });

  const form = useForm<ProviderFormValues>({
    resolver: zodResolver(providerFormSchema),
    defaultValues: { name: "", type: "deepseek", baseUrl: "", apiKey: "" },
  });

  const watchedType = form.watch("type");
  const providerHint = getProviderHint(watchedType);

  // 打开时按模式预填：编辑回填既有值（Key 不回显）；新增重置为空表单。
  useEffect(() => {
    if (open) {
      form.reset({
        name: provider?.name ?? "",
        type: provider?.type ?? "deepseek",
        baseUrl: provider?.base_url ?? "",
        apiKey: "",
      });
      setError(null);
      setClearKey(false);
      setTestState({ pending: false, result: null });
    }
  }, [open, provider, form]);

  /**
   * 提交：透传表单值与清除标记（空串归一化由调用方按创建 null / 编辑 "" 语义处理）。
   *
   * 错误展示：调用方（ProviderSettingsDialog.handleFormSubmit）失败时抛出真实的
   * ``ServiceError``；这里优先展示其 ``message``（后端返回的可读错误，如唯一约束冲突、
   * 内部错误根因）并附 ``statusCode``，不再回退到「名称重复 / 网络不可用」这种与
   * 根因无关的写死兜底文案——历史上曾因 SQLite 缺列导致 500 却被误导为名称重复。
   * 非 ServiceError 的意外异常才回退到通用提示。
   */
  const handleSubmit = async (values: ProviderFormValues) => {
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(values, { clearKey });
    } catch (err) {
      if (err instanceof ServiceError) {
        const status = err.statusCode ? `（HTTP ${err.statusCode}）` : "";
        setError(`保存失败${status}：${err.message}`);
      } else {
        setError("保存失败，请稍后重试");
      }
      return;
    } finally {
      setSubmitting(false);
    }
  };

  /**
   * 测试连接：调 POST /providers/{id}/test 验证凭据 / 端点可用性。
   *
   * 测试本身就是「试错」语义：失败是正常结果之一，结果就地展示
   * 成功（✓ + 耗时） / 失败（✗ + 可读消息），不关闭对话框。
   * 骨架阶段后端可能返回 501 Not Implemented，由 ServiceError 承接展示。
   */
  const handleTestConnection = async () => {
    if (!provider) return;
    setTestState({ pending: true, result: null });
    try {
      const result = await testProviderConnection(provider.provider_id);
      setTestState({
        pending: false,
        result: {
          success: result.success,
          message: result.success
            ? `连接成功（${result.elapsed_ms}ms）`
            : (result.error_message ?? "连接失败"),
        },
      });
    } catch (err) {
      // 骨架阶段后端返回 501 或网络错误：ServiceError.message 已可读，直接展示。
      logError("测试厂商连通性失败", err, {
        module: "ProviderFormDialog",
        provider_id: provider.provider_id,
      });
      setTestState({
        pending: false,
        result: {
          success: false,
          message: err instanceof Error ? err.message : "测试失败（后端未实现或网络错误）",
        },
      });
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogTitle>{isEdit ? "编辑厂商" : "新增厂商"}</DialogTitle>
        <DialogDescription>保存后即可导入该厂商下的模型。</DialogDescription>

        <Form {...form}>
          <form onSubmit={form.handleSubmit(handleSubmit)} className="space-y-4">
            <FormField
              name="name"
              control={form.control}
              render={({ field }) => (
                <FormItem>
                  <FormLabel>名称</FormLabel>
                  <FormControl>
                    <Input placeholder="DeepSeek" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              name="type"
              control={form.control}
              render={({ field }) => (
                <FormItem>
                  <FormLabel>类型</FormLabel>
                  <Select value={field.value} onValueChange={field.onChange}>
                    <FormControl>
                      <SelectTrigger>
                        <SelectValue placeholder="选择厂商类型" />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      {PROVIDER_TYPE_OPTIONS.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              name="baseUrl"
              control={form.control}
              render={({ field }) => (
                <FormItem>
                  <FormLabel>API 端点（可选）</FormLabel>
                  <FormControl>
                    <Input placeholder="https://api.example.com/v1" {...field} />
                  </FormControl>
                  {providerHint && (
                    <p className="text-xs text-muted-foreground">{providerHint}</p>
                  )}
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              name="apiKey"
              control={form.control}
              render={({ field }) => (
                <FormItem>
                  <FormLabel>API Key</FormLabel>
                  <FormControl>
                    <Input
                      type="password"
                      autoComplete="off"
                      placeholder="选填"
                      {...field}
                    />
                  </FormControl>
                  {isEdit && (
                    <p
                      className={
                        provider?.api_key_configured
                          ? "text-xs text-emerald-500"
                          : "text-xs text-amber-500"
                      }
                    >
                      {provider?.api_key_configured ? "Key 已设置" : "Key 未设置"}
                      {clearKey ? (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="ml-2 h-6 px-2 text-destructive"
                          onClick={() => setClearKey(false)}
                        >
                          取消清除
                        </Button>
                      ) : (
                        provider?.api_key_configured && (
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            className="ml-2 h-6 px-2 text-muted-foreground"
                            onClick={() => setClearKey(true)}
                          >
                            清除
                          </Button>
                        )
                      )}
                    </p>
                  )}
                  <p className="text-xs text-muted-foreground">
                    {isEdit
                      ? "编辑时留空表示不修改；仅保存在本地数据库。"
                      : "仅保存在本地数据库；不依赖 Key 的厂商可留空。"}
                  </p>
                  <FormMessage />
                </FormItem>
              )}
            />

            {/* 测试连接按钮 + 结果展示（仅编辑模式 + 已有 provider_id 时可见） */}
            {isEdit && provider && (
              <div className="space-y-1.5">
                <div className="flex items-center gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => void handleTestConnection()}
                    disabled={testState.pending || submitting}
                  >
                    {testState.pending ? (
                      <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                    ) : (
                      "测试连接"
                    )}
                  </Button>
                  {testState.result && (
                    <span
                      className={
                        testState.result.success
                          ? "flex items-center gap-1 text-xs text-emerald-500"
                          : "flex items-center gap-1 text-xs text-destructive"
                      }
                    >
                      {testState.result.success ? (
                        <Check className="h-3.5 w-3.5" />
                      ) : (
                        <X className="h-3.5 w-3.5" />
                      )}
                      {testState.result.message}
                    </span>
                  )}
                </div>
                <p className="text-xs text-muted-foreground">验证当前配置是否可正常连接。</p>
              </div>
            )}

            {error && <p className="text-xs text-destructive">{error}</p>}

            <DialogFooter>
              <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
                取消
              </Button>
              <Button type="submit" variant="primary" disabled={submitting}>
                {submitting && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
                {isEdit ? "保存" : "创建"}
              </Button>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}
