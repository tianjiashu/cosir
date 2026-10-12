import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  CheckCircle2Icon,
  CheckIcon,
  CirclePauseIcon,
  CircleXIcon,
  ClipboardIcon,
  Clock3Icon,
  GaugeIcon,
  LoaderCircleIcon,
  RefreshCwIcon,
  SearchIcon,
  ArrowDownIcon,
  ArrowUpIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getContextMessages, type ContextMessage } from "@/lib/api/context-messages";

function jsonText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined) return "未提供";
  return JSON.stringify(value, null, 2);
}

function preview(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text?.replace(/\s+/g, " ").slice(0, 180) ?? "";
}

function formatTimestamp(value: string, compact = false): string {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return value;
  const options: Intl.DateTimeFormatOptions = compact
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 3, hourCycle: "h23" }
    : { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 3, hourCycle: "h23" };
  return new Intl.DateTimeFormat("zh-CN", options).format(timestamp);
}

function formatTokens(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

function cacheUsage(message: ContextMessage): { rate: number; hit: number; input: number } | null {
  const usage = message.usage_metadata;
  const inputDetails = usage?.input_token_details;
  const input = usage?.input_tokens;
  const hit = inputDetails && typeof inputDetails === "object"
    ? (inputDetails as Record<string, unknown>).cache_read
    : undefined;
  if (
    typeof input !== "number" || !Number.isFinite(input) || input <= 0
    || typeof hit !== "number" || !Number.isFinite(hit) || hit < 0 || hit > input
  ) return null;
  return { rate: Math.round((hit / input) * 1000) / 10, hit, input };
}

function toolNames(message: ContextMessage): string[] {
  return (message.tool_calls ?? [])
    .map((call) => call.name)
    .filter((name): name is string => typeof name === "string" && name.length > 0);
}

function statusPresentation(status: string | null | undefined): {
  label: string;
  className: string;
  icon: typeof CheckCircle2Icon;
} | null {
  if (!status) return { label: "状态未记录", className: "border-border bg-muted text-muted-foreground", icon: Clock3Icon };
  switch (status) {
    case "completed": return { label: "已完成", className: "border-emerald-700/20 bg-emerald-500/10 text-emerald-800 dark:text-emerald-300", icon: CheckCircle2Icon };
    case "failed": return { label: "失败", className: "border-destructive/25 bg-destructive/10 text-destructive", icon: CircleXIcon };
    case "cancelled": return { label: "已取消", className: "border-amber-700/20 bg-amber-500/10 text-amber-800 dark:text-amber-300", icon: CirclePauseIcon };
    case "running": return { label: "执行中", className: "border-sky-700/20 bg-sky-500/10 text-sky-800 dark:text-sky-300", icon: LoaderCircleIcon };
    case "pending": return { label: "待执行", className: "border-border bg-muted text-muted-foreground", icon: Clock3Icon };
    default: return { label: status, className: "border-border bg-muted text-muted-foreground", icon: Clock3Icon };
  }
}

function roleLabel(role: string): string {
  switch (role) {
    case "system": return "System";
    case "assistant": return "AI";
    case "tool": return "Tool";
    case "user":
    case "human": return "Human";
    default: return role;
  }
}

function roleStyle(role: string): string {
  switch (role) {
    case "system": return "border-l-amber-500";
    case "assistant": return "border-l-sky-600";
    case "tool": return "border-l-emerald-600";
    case "user":
    case "human": return "border-l-violet-500";
    default: return "border-l-muted-foreground";
  }
}

function roleBadgeStyle(role: string): string {
  switch (role) {
    case "system": return "bg-amber-500/10 text-amber-800 dark:text-amber-300";
    case "assistant": return "bg-sky-500/10 text-sky-800 dark:text-sky-300";
    case "tool": return "bg-emerald-500/10 text-emerald-800 dark:text-emerald-300";
    case "user":
    case "human": return "bg-violet-500/10 text-violet-800 dark:text-violet-300";
    default: return "bg-muted text-muted-foreground";
  }
}

type RoleFilter = "all" | "assistant" | "system" | "human" | "tool";
type SortKey = "sequence" | "created_at" | "updated_at";
const cacheDeclineAlertThreshold = 5;

function Field({ name, value }: { name: string; value: unknown }) {
  return (
    <section className="min-w-0 border-b last:border-b-0">
      <h3 className="px-4 pt-3 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">{name}</h3>
      <pre className="max-h-[34vh] overflow-auto whitespace-pre-wrap break-words px-4 py-3 font-mono text-xs leading-5 text-foreground">{jsonText(value)}</pre>
    </section>
  );
}

export function ContextObserverPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [taskInput, setTaskInput] = useState(searchParams.get("task_id") ?? "");
  const [runInput, setRunInput] = useState(searchParams.get("run_id") ?? "");
  const [messages, setMessages] = useState<ContextMessage[]>([]);
  const [selectedSequence, setSelectedSequence] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [refreshGeneration, setRefreshGeneration] = useState(0);
  const [roleFilter, setRoleFilter] = useState<RoleFilter>("all");
  const [sortKey, setSortKey] = useState<SortKey>("sequence");
  const [sortAscending, setSortAscending] = useState(true);
  const listRef = useRef<HTMLDivElement>(null);

  const taskId = Number(searchParams.get("task_id"));
  const runId = Number(searchParams.get("run_id"));
  const queryKey = `${Number.isInteger(taskId) && taskId > 0 ? taskId : ""}:${Number.isInteger(runId) && runId > 0 ? runId : ""}`;

  useEffect(() => {
    setTaskInput(searchParams.get("task_id") ?? "");
    setRunInput(searchParams.get("run_id") ?? "");
  }, [searchParams]);

  useEffect(() => {
    if (!taskId && !runId) {
      setMessages([]);
      setSelectedSequence(null);
      setError(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void getContextMessages({
      ...(Number.isInteger(taskId) && taskId > 0 ? { taskId } : {}),
      ...(Number.isInteger(runId) && runId > 0 ? { runId } : {}),
    }, { signal: controller.signal })
      .then((result) => {
        setMessages(result);
        setSelectedSequence((current) => result.some((message) => message.sequence === current)
          ? current
          : result[0]?.sequence ?? null);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setMessages([]);
        setSelectedSequence(null);
        setError(cause instanceof Error ? cause.message : "上下文读取失败");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [queryKey, taskId, runId, refreshGeneration]);

  const cacheDeclines = useMemo(() => {
    const aiMessages = messages
      .filter((message) => message.role === "assistant")
      .sort((left, right) => left.sequence - right.sequence);
    const declines = new Map<number, number>();
    let previousRate: number | null = null;
    for (const message of aiMessages) {
      const usage = cacheUsage(message);
      if (!usage) continue;
      if (previousRate !== null && usage.rate < previousRate) {
        declines.set(message.sequence, previousRate - usage.rate);
      }
      previousRate = usage.rate;
    }
    return declines;
  }, [messages]);
  const visibleMessages = useMemo(() => {
    const filtered = messages.filter((message) => {
      if (roleFilter === "all") return true;
      if (roleFilter === "human") return message.role === "user" || message.role === "human";
      return message.role === roleFilter;
    });
    const direction = sortAscending ? 1 : -1;
    return filtered.sort((left, right) => {
      const leftValue = sortKey === "sequence" ? left.sequence : Date.parse(left[sortKey]);
      const rightValue = sortKey === "sequence" ? right.sequence : Date.parse(right[sortKey]);
      const comparison = (Number.isNaN(leftValue) ? 0 : leftValue) - (Number.isNaN(rightValue) ? 0 : rightValue);
      return comparison === 0
        ? (left.sequence - right.sequence) * direction
        : comparison * direction;
    });
  }, [messages, roleFilter, sortAscending, sortKey]);
  const sequenceRange = useMemo(() => messages.reduce<{ min: number; max: number } | null>(
    (range, message) => range
      ? { min: Math.min(range.min, message.sequence), max: Math.max(range.max, message.sequence) }
      : { min: message.sequence, max: message.sequence },
    null,
  ), [messages]);
  const selected = useMemo(
    () => visibleMessages.find((message) => message.sequence === selectedSequence) ?? null,
    [visibleMessages, selectedSequence],
  );
  useEffect(() => {
    if (visibleMessages.some((message) => message.sequence === selectedSequence)) return;
    setSelectedSequence(visibleMessages[0]?.sequence ?? null);
  }, [visibleMessages, selectedSequence]);
  const virtualizer = useVirtualizer({
    count: visibleMessages.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => 128,
    overscan: 8,
  });

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setValidationError(null);
    const taskValue = taskInput.trim();
    const runValue = runInput.trim();
    const parseId = (value: string) => /^\d+$/.test(value) && Number(value) > 0 ? Number(value) : null;
    const nextTaskId = taskValue ? parseId(taskValue) : null;
    const nextRunId = runValue ? parseId(runValue) : null;
    if (taskValue && nextTaskId === null) {
      setValidationError("Task ID 必须是正整数。");
      return;
    }
    if (runValue && nextRunId === null) {
      setValidationError("Run ID 必须是正整数。");
      return;
    }
    if (nextTaskId === null && nextRunId === null) {
      setValidationError("请输入 Task ID 或 Run ID。");
      return;
    }
    const params = new URLSearchParams();
    if (nextTaskId !== null) params.set("task_id", String(nextTaskId));
    if (nextRunId !== null) params.set("run_id", String(nextRunId));
    setSearchParams(params);
    setRefreshGeneration((current) => current + 1);
  };

  const copySelected = async () => {
    if (!selected) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(selected, null, 2));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      setError("复制失败，请检查桌面应用的剪贴板权限。");
    }
  };

  const refresh = () => setRefreshGeneration((current) => current + 1);

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <form onSubmit={submit} className="flex shrink-0 flex-wrap items-end gap-3 border-b px-5 py-4">
        <div className="min-w-40 flex-1 basis-48">
          <label htmlFor="context-task-id" className="mb-1.5 block text-xs font-medium text-muted-foreground">Task ID</label>
          <Input id="context-task-id" inputMode="numeric" value={taskInput} onChange={(event) => setTaskInput(event.target.value)} placeholder="例如 42" />
        </div>
        <div className="min-w-40 flex-1 basis-48">
          <label htmlFor="context-run-id" className="mb-1.5 block text-xs font-medium text-muted-foreground">Run ID</label>
          <Input id="context-run-id" inputMode="numeric" value={runInput} onChange={(event) => setRunInput(event.target.value)} placeholder="例如 21" />
        </div>
        <Button type="submit" className="gap-2"><SearchIcon />查询</Button>
        <Button type="button" variant="outline" size="icon" onClick={refresh} disabled={loading || (!taskId && !runId)} aria-label="刷新上下文"><RefreshCwIcon className={loading ? "animate-spin" : ""} /></Button>
        {validationError && <p role="alert" className="basis-full text-xs text-destructive">{validationError}</p>}
      </form>

      <div className="flex min-h-0 flex-1 flex-col xl:flex-row">
        <section className="flex min-h-0 min-w-0 flex-1 flex-col xl:w-[56%] xl:border-r" aria-label="上下文消息列表">
          <div className="shrink-0 space-y-3 border-b px-5 py-3">
            <div className="flex items-baseline justify-between gap-3">
              <h2 className="text-sm font-semibold">消息序列</h2>
              <span className="shrink-0 font-mono text-[11px] tabular-nums text-muted-foreground">
                {loading ? "正在读取…" : roleFilter === "all" ? `${messages.length} 条` : `${visibleMessages.length} / ${messages.length} 条`}
                {sequenceRange && ` · #${sequenceRange.min}–${sequenceRange.max}`}
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <label htmlFor="context-role-filter" className="sr-only">按角色筛选</label>
              <select id="context-role-filter" value={roleFilter} onChange={(event) => setRoleFilter(event.target.value as RoleFilter)} className="h-8 rounded-md border bg-background px-2 text-xs text-foreground">
                <option value="all">全部角色</option>
                <option value="assistant">AI</option>
                <option value="system">System</option>
                <option value="human">Human</option>
                <option value="tool">Tool</option>
              </select>
              <label htmlFor="context-sort-key" className="sr-only">排序依据</label>
              <select id="context-sort-key" value={sortKey} onChange={(event) => setSortKey(event.target.value as SortKey)} className="h-8 rounded-md border bg-background px-2 text-xs text-foreground">
                <option value="sequence">按序号</option>
                <option value="created_at">按创建时间</option>
                <option value="updated_at">按更新时间</option>
              </select>
              <Button type="button" variant="outline" size="sm" className="h-8 gap-1.5 px-2 text-xs" onClick={() => setSortAscending((ascending) => !ascending)} aria-label={sortAscending ? "切换为降序" : "切换为升序"}>
                {sortAscending ? <ArrowUpIcon /> : <ArrowDownIcon />}{sortAscending ? "升序" : "降序"}
              </Button>
            </div>
          </div>
          <div ref={listRef} className="min-h-0 flex-1 overflow-auto">
            {error && <div role="alert" className="m-5 border-l-2 border-destructive px-3 py-2 text-sm text-destructive">{error}</div>}
            {!loading && !error && messages.length === 0 && (
              <div className="mx-auto max-w-md px-6 py-16 text-center">
                <p className="text-sm font-medium">{taskId || runId ? "没有找到上下文消息" : "输入 Task ID 或 Run ID 开始查询"}</p>
                <p className="mt-2 text-xs leading-5 text-muted-foreground">结果包含流式草稿和未纳入模型上下文的记录，时间按消息创建和更新时刻展示。</p>
              </div>
            )}
            {!loading && !error && messages.length > 0 && visibleMessages.length === 0 && <p className="px-5 py-8 text-center text-sm text-muted-foreground">当前角色筛选下没有消息。</p>}
            {visibleMessages.length > 0 && (
              <div className="relative" style={{ height: `${virtualizer.getTotalSize()}px` }}>
                {virtualizer.getVirtualItems().map((virtualRow) => {
                  const message = visibleMessages[virtualRow.index];
                  const active = message.sequence === selectedSequence;
                  const cache = message.role === "assistant" ? cacheUsage(message) : null;
                  const cacheDecline = cacheDeclines.get(message.sequence);
                  const callNames = message.role === "assistant" ? toolNames(message) : [];
                  const toolStatus = message.role === "tool" ? statusPresentation(message.status) : null;
                  const StatusIcon = toolStatus?.icon;
                  return (
                    <button
                      key={`${message.task_id}-${message.sequence}`}
                      ref={virtualizer.measureElement}
                      data-index={virtualRow.index}
                      type="button"
                      onClick={() => setSelectedSequence(message.sequence)}
                      aria-pressed={active}
                      className={`absolute left-0 top-0 flex min-h-24 w-full flex-col gap-1 border-b border-l-2 px-4 py-2.5 text-left transition-colors ${roleStyle(message.role)} ${active ? "bg-accent/80 ring-1 ring-inset ring-foreground/10" : "hover:bg-muted/60"}`}
                      style={{ transform: `translateY(${virtualRow.start}px)` }}
                    >
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="w-12 shrink-0 font-mono text-xs text-muted-foreground">#{message.sequence}</span>
                        <span className={`rounded-sm px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${roleBadgeStyle(message.role)}`}>{roleLabel(message.role)}</span>
                        <span className="truncate font-mono text-[10px] text-muted-foreground">Run {message.run_id ?? "—"}</span>
                        {message.role === "assistant" && <span title={cache ? `缓存命中 ${formatTokens(cache.hit)} / 输入 ${formatTokens(cache.input)} tokens${cacheDecline === undefined ? "" : `；较上一条可计算记录下降 ${cacheDecline.toFixed(1)} 个百分点`}` : "供应商未提供可计算缓存率的输入明细"} className={`ml-auto inline-flex shrink-0 items-center gap-1 rounded-sm border px-2 py-1 text-[11px] font-semibold tabular-nums ${cache ? cacheDecline !== undefined && cacheDecline > cacheDeclineAlertThreshold ? "border-destructive/30 bg-destructive/10 text-destructive" : "border-sky-700/20 bg-sky-500/10 text-sky-800 dark:text-sky-300" : "border-border bg-muted text-muted-foreground"}`}><GaugeIcon className="size-3.5" aria-hidden="true" />缓存 {cache ? `${cache.rate.toFixed(1)}%` : "—"}{cacheDecline !== undefined && cacheDecline > cacheDeclineAlertThreshold && <span className="ml-0.5">↓{cacheDecline.toFixed(1)} 个百分点</span>}</span>}
                        {toolStatus && StatusIcon && <span title={message.status ?? undefined} className={`ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-sm border px-2 py-1 text-[11px] font-semibold ${toolStatus.className}`}><StatusIcon className={`size-3.5 ${message.status === "running" ? "animate-spin" : ""}`} aria-hidden="true" />{toolStatus.label}</span>}
                      </div>
                      <p className="line-clamp-2 pl-12 text-xs leading-4 text-muted-foreground">{preview(message.content) || "（空内容）"}</p>
                      {callNames.length > 0 && <div className="flex min-w-0 flex-wrap items-center gap-1 pl-12 text-[10px]"><span className="shrink-0 text-muted-foreground">调用工具</span>{callNames.map((name, index) => <span key={`${name}-${index}`} className="max-w-full truncate rounded-sm border border-sky-700/15 bg-sky-500/5 px-1.5 py-0.5 font-mono text-sky-800 dark:text-sky-300">{name}</span>)}</div>}
                      <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-0.5 pl-12 text-[11px] font-medium tabular-nums text-foreground/75">
                        <span className="inline-flex min-w-0 items-center gap-1 truncate" title={message.created_at}><Clock3Icon className="size-3 shrink-0 text-muted-foreground" aria-hidden="true" /><span className="text-muted-foreground">创建</span><time dateTime={message.created_at}>{formatTimestamp(message.created_at, true)}</time></span><span className="inline-flex min-w-0 items-center gap-1 truncate" title={message.updated_at}><span className="text-muted-foreground">更新</span><time dateTime={message.updated_at}>{formatTimestamp(message.updated_at, true)}</time></span>
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        </section>

        <section className="flex min-h-0 min-w-0 flex-1 flex-col xl:w-[44%]" aria-label="消息结构详情">
          <div className="flex min-h-14 shrink-0 items-center justify-between border-b px-4 py-2">
            <div className="min-w-0">
              <h2 className="text-sm font-semibold">结构化详情</h2>
              {selected && <p className="truncate font-mono text-[10px] text-muted-foreground">Task {selected.task_id} · Run {selected.run_id ?? "—"} · sequence {selected.sequence}</p>}
            </div>
            <Button type="button" variant="ghost" size="sm" className="gap-1.5" onClick={() => void copySelected()} disabled={!selected}>
              {copied ? <CheckIcon /> : <ClipboardIcon />}{copied ? "已复制" : "复制 JSON"}
            </Button>
          </div>
          {selected ? (
            <div className="min-h-0 flex-1 overflow-auto">
              <dl className="grid grid-cols-[8rem_minmax(0,1fr)] border-b text-xs">
                <dt className="border-r px-4 py-2.5 text-muted-foreground">role</dt><dd className="px-4 py-2.5 font-mono">{selected.role}</dd>
                <dt className="border-r border-t px-4 py-2.5 text-muted-foreground">created_at</dt><dd className="break-all px-4 py-2.5 font-mono"><time dateTime={selected.created_at}>{formatTimestamp(selected.created_at)}</time></dd>
                <dt className="border-r border-t px-4 py-2.5 text-muted-foreground">updated_at</dt><dd className="break-all px-4 py-2.5 font-mono"><time dateTime={selected.updated_at}>{formatTimestamp(selected.updated_at)}</time></dd>
                {selected.role === "tool" && <><dt className="border-r border-t px-4 py-2.5 text-muted-foreground">tool_call_id</dt><dd className="break-all px-4 py-2.5 font-mono">{selected.tool_call_id ?? "未记录"}</dd></>}
                {selected.role === "tool" && <><dt className="border-r border-t px-4 py-2.5 text-muted-foreground">status</dt><dd className="px-4 py-2.5 font-mono">{selected.status ?? "未记录"}</dd></>}
              </dl>
              <Field name="content" value={selected.content} />
              {selected.role === "assistant" && <>
                <Field name="tool_calls" value={selected.tool_calls ?? []} />
                <Field name="invalid_tool_calls" value={selected.invalid_tool_calls ?? []} />
                <Field name="usage_metadata" value={selected.usage_metadata ?? null} />
              </>}
            </div>
          ) : (
            <div className="flex min-h-40 flex-1 items-center justify-center px-6 text-center text-xs text-muted-foreground">选择一条消息查看完整字段。</div>
          )}
        </section>
      </div>
    </div>
  );
}
