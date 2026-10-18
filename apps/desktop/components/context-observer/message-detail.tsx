import { useMemo, useRef, useState } from "react";
import { ArrowLeftIcon, CopyIcon, WrapTextIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { ReadonlyTextViewer } from "@/components/text-viewer/readonly-text-viewer";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import type { ContextMessage } from "@/lib/api/context-messages";
import {
  cacheUsage,
  formatTime,
  relatedMessages,
  roleLabel,
  toolStatus,
  valueText,
} from "./message-model";

type DetailTab = "content" | "tools" | "usage" | "json";
const formatTokens = (value: unknown) =>
  typeof value === "number"
    ? new Intl.NumberFormat("zh-CN").format(value)
    : "未提供";

/** 详情只投影接口字段；页签和滚动是阅读状态，不回写上下文或工具生命周期。 */
export function MessageDetail({
  message,
  messages,
  onLocate,
  onBack,
  narrow,
}: {
  message: ContextMessage;
  messages: ContextMessage[];
  onLocate: (sequence: number) => void;
  onBack: () => void;
  narrow: boolean;
}) {
  const [preferredTab, setTab] = useState<DetailTab>("content");
  const [wrap, setWrap] = useState(true);
  const scrollPositions = useRef(new Map<string, number>());
  const hasTools =
    message.role === "tool" ||
    Boolean(message.tool_calls?.length);
  const tabs: { value: DetailTab; label: string }[] = [
    { value: "content", label: "内容" },
    ...(hasTools ? [{ value: "tools" as const, label: "工具调用" }] : []),
    ...(message.role === "assistant"
      ? [{ value: "usage" as const, label: "用量" }]
      : []),
    { value: "json", label: "JSON" },
  ];
  const tab = tabs.some((item) => item.value === preferredTab)
    ? preferredTab
    : "content";
  const readingKey = `${message.sequence}:${tab}`;
  const related = useMemo(
    () => relatedMessages(message, messages),
    [message, messages],
  );
  const text = useMemo(
    () =>
      tab === "json"
        ? JSON.stringify(message, null, 2)
        : tab === "content"
          ? valueText(message.content)
          : tab === "usage"
            ? valueText(message.usage_metadata ?? null)
            : valueText(
                message.role === "tool"
                  ? {
                      tool_call_id: message.tool_call_id,
                      status: message.status,
                      content: message.content,
                    }
                  : {
                      tool_calls: message.tool_calls ?? [],
                    },
              ),
    [message, tab],
  );
  const plainText = tab === "content" && typeof message.content === "string";
  const usage = message.usage_metadata;
  const cache = cacheUsage(message);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      notify.success(tab === "content" ? "已复制内容" : "已复制 JSON");
    } catch (error) {
      notify.error("复制失败", { error, fallback: "无法写入剪贴板" });
      void frontendLog(
        "WARNING",
        "context_message_copy_failed",
        "复制上下文记录失败",
        {
          data: { taskId: message.task_id, sequence: message.sequence, tab },
          error,
        },
      );
    }
  };
  return (
    <section className="flex h-full min-h-0 flex-col" aria-label="消息详情">
      <div className="shrink-0 space-y-3 border-b px-5 py-3">
        {narrow && (
          <Button size="sm" variant="ghost" onClick={onBack}>
            <ArrowLeftIcon />
            返回列表
          </Button>
        )}
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-sm font-semibold">
            <span className="mr-2 font-mono">#{message.sequence}</span>
            {roleLabel(message.role)}
          </h2>
        </div>
        <details className="text-xs" key={message.sequence}>
          <summary className="w-fit cursor-pointer text-muted-foreground focus-ring">
            消息元信息
          </summary>
          <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 break-all">
            <dt className="text-muted-foreground">Task / sequence</dt>
            <dd className="font-mono">
              {message.task_id} / {message.sequence}
            </dd>
            <dt className="text-muted-foreground">创建时间</dt>
            <dd>{formatTime(message.created_at, true)}</dd>
            <dt className="text-muted-foreground">更新时间</dt>
            <dd>{formatTime(message.updated_at, true)}</dd>
            {message.role === "tool" && (
              <>
                <dt className="text-muted-foreground">调用 ID</dt>
                <dd className="font-mono">
                  {message.tool_call_id ?? "未记录"}
                </dd>
                <dt className="text-muted-foreground">工具状态</dt>
                <dd>{toolStatus(message.status)}</dd>
              </>
            )}
          </dl>
        </details>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <SegmentedControl
            size="sm"
            aria-label="消息详情视图"
            value={tab}
            options={tabs}
            onValueChange={setTab}
          />
          <div className="flex gap-1">
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label="自动换行"
              aria-pressed={wrap}
              onClick={() => setWrap((value) => !value)}
            >
              <WrapTextIcon />
            </Button>
            <Button size="sm" variant="ghost" onClick={() => void copy()}>
              <CopyIcon />
              {tab === "content" ? "复制内容" : "复制 JSON"}
            </Button>
          </div>
        </div>
      </div>
      {tab === "tools" && (
        <div className="max-h-44 shrink-0 space-y-2 overflow-auto border-b px-5 py-3 text-xs">
          {message.role === "tool" ? (
            <>
              <p className="text-muted-foreground">对应的 Assistant 调用记录</p>
              {related.length ? (
                related.map((item) => (
                  <Button
                    key={item.sequence}
                    size="sm"
                    variant="outline"
                    onClick={() => onLocate(item.sequence)}
                  >
                    查看调用 #{item.sequence}
                  </Button>
                ))
              ) : (
                <p>当前查询范围没有对应调用记录。</p>
              )}
            </>
          ) : (
            <>
              {message.tool_calls?.map((call, index) => {
                const results = related.filter(
                  (item) => item.tool_call_id === call.id,
                );
                return (
                  <div
                    key={index}
                    className="flex flex-wrap items-center gap-2"
                  >
                    <span className="font-mono">
                      {typeof call.name === "string" ? call.name : "未命名工具"}
                    </span>
                    {results.length ? (
                      results.map((item) => (
                        <Button
                          key={item.sequence}
                          size="sm"
                          variant="outline"
                          onClick={() => onLocate(item.sequence)}
                        >
                          查看结果 #{item.sequence}
                        </Button>
                      ))
                    ) : (
                      <span className="text-muted-foreground">
                        当前查询范围没有对应结果
                      </span>
                    )}
                  </div>
                );
              })}
            </>
          )}
        </div>
      )}
      {tab === "usage" && (
        <div className="shrink-0 border-b px-5 py-3 text-xs">
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {[
              ["输入 tokens", usage?.input_tokens],
              ["输出 tokens", usage?.output_tokens],
              ["缓存读取 tokens", cache?.hit],
              ["总计 tokens", usage?.total_tokens],
            ].map(([label, value]) => (
              <div key={String(label)}>
                <dt className="text-muted-foreground">{String(label)}</dt>
                <dd className="mt-1 font-mono">{formatTokens(value)}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}
      <div
        className="min-h-0 flex-1 text-xs"
        data-testid="context-detail-content"
      >
        <ReadonlyTextViewer
          key={readingKey}
          text={text}
          name={plainText ? "message.txt" : "message.json"}
          ariaLabel={`${tabs.find((item) => item.value === tab)!.label} · 消息 #${message.sequence}`}
          wrap={wrap}
          scrollTop={scrollPositions.current.get(readingKey) ?? 0}
          onScroll={(top) => {
            scrollPositions.current.set(readingKey, top);
          }}
        />
      </div>
    </section>
  );
}
