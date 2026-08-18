/**
 * 模型厂商新增/编辑表单对话框（配置中心子组件，设计 §10.2）。
 *
 * react-hook-form + zod 校验：名称（必填）、类型（5 预设）、base_url（选填，
 * 空则用 litellm 内置解析）、api_key（选填明文，password 输入框；Key 明文
 * 保存于后端 DB providers.api_key，DB 为唯一事实来源，响应与日志不回传明文）。
 *
 * 编辑模式展示既有厂商的 Key 配置状态（api_key_configured，聚合自后端），
 * 不回显明文；输入为空时提交不更新 Key，可显式点击「清除」标记提交时清空。
 *
 * @module components/settings/ProviderFormDialog
 */

import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Loader2 } from "lucide-react";
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

/** 厂商类型预设（与后端 PROVIDER_TYPES 枚举对齐）。 */
const PROVIDER_TYPE_OPTIONS: { value: ProviderType; label: string }[] = [
  { value: "deepseek", label: "DeepSeek（官方）" },
  { value: "openai-compatible", label: "OpenAI 兼容接口" },
  { value: "anthropic", label: "Anthropic" },
  { value: "ollama", label: "Ollama（本地）" },
  { value: "custom", label: "自定义" },
];

/** 厂商表单校验 schema（zod v4；base_url 允许空 = 用 litellm 内置解析）。 */
const providerFormSchema = z.object({
  name: z.string().min(1, "请输入厂商名称"),
  type: z.enum(["deepseek", "openai-compatible", "anthropic", "ollama", "custom"]),
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
   * @returns 提交成功返回 true；失败返回 false（错误展示由本组件 error 态承接）。
   */
  onSubmit: (values: ProviderFormValues, meta: ProviderFormMeta) => Promise<boolean>;
}

/**
 * 厂商新增/编辑表单对话框。
 *
 * 编辑模式下用既有厂商预填表单（apiKey 始终为空，不回显明文）；提交成功后
 * 由父组件关闭并刷新列表。Key 状态徽标与「清除」按钮仅编辑模式展示。
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

  const form = useForm<ProviderFormValues>({
    resolver: zodResolver(providerFormSchema),
    defaultValues: { name: "", type: "deepseek", baseUrl: "", apiKey: "" },
  });

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
    }
  }, [open, provider, form]);

  /** 提交：透传表单值与清除标记（空串归一化由调用方按创建 null / 编辑 "" 语义处理）。 */
  const handleSubmit = async (values: ProviderFormValues) => {
    setSubmitting(true);
    setError(null);
    const ok = await onSubmit(values, { clearKey });
    setSubmitting(false);
    if (!ok) {
      setError("保存失败，请检查名称是否重复或网络是否可用");
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogTitle>{isEdit ? "编辑厂商" : "新增厂商"}</DialogTitle>
        <DialogDescription>
          厂商决定模型路由前缀与 API Key 读取方式；保存后需在展开区导入模型条目。
        </DialogDescription>

        <Form {...form}>
          <form onSubmit={form.handleSubmit(handleSubmit)} className="space-y-4">
            <FormField
              name="name"
              control={form.control}
              render={({ field }) => (
                <FormItem>
                  <FormLabel>名称</FormLabel>
                  <FormControl>
                    <Input placeholder="如 DeepSeek 官方" {...field} />
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
                  <FormLabel>base_url（可选）</FormLabel>
                  <FormControl>
                    <Input placeholder="留空使用 litellm 内置解析，如 https://api.example.com/v1" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              name="apiKey"
              control={form.control}
              render={({ field }) => (
                <FormItem>
                  <FormLabel>API Key（可选）</FormLabel>
                  <FormControl>
                    <Input
                      type="password"
                      autoComplete="off"
                      placeholder="本地 Ollama 可留空；保存后不回显明文"
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
                      ? "编辑时留空表示不修改已保存的 Key；Key 明文保存在后端数据库（本地单机），响应与日志不回传明文。"
                      : "Key 明文保存在后端数据库（本地单机）；不依赖 Key 的厂商（如本地 Ollama）可留空。"}
                  </p>
                  <FormMessage />
                </FormItem>
              )}
            />

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
