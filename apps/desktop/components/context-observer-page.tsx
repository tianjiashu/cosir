import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  CheckCircle2Icon,
  CheckIcon,
  CirclePauseIcon,
  CircleXIcon,
  ClipboardIcon,
  Clock3Icon,
  ArrowLeftIcon,
  GaugeIcon,
  LoaderCircleIcon,
  ListTreeIcon,
  RefreshCwIcon,
  SearchIcon,
  Undo2Icon,
  ArrowDownIcon,
  ArrowUpIcon,
} from "lucide-react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { EmptyState, ErrorState } from "@/components/ui/state-panel";
import { getContextMessages, type ContextMessage, type ContextMessageQuery } from "@/lib/api/context-messages";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";

const NO_MESSAGES: ContextMessage[] = [];

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
  tone: BadgeTone;
  icon: typeof CheckCircle2Icon;
} | null {
  if (!status) return { label: "状态未记录", tone: "neutral", icon: Clock3Icon };
  switch (status) {
    case "completed": return { label: "已完成", tone: "success", icon: CheckCircle2Icon };
    case "failed": return { label: "失败", tone: "destructive", icon: CircleXIcon };
    case "cancelled": return { label: "已取消", tone: "warning", icon: CirclePauseIcon };
    case "running": return { label: "执行中", tone: "info", icon: LoaderCircleIcon };
    case "pending": return { label: "待执行", tone: "neutral", icon: Clock3Icon };
    default: return { label: status, tone: "neutral", icon: Clock3Icon };
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
    case "system": return "border-l-warning";
    case "assistant": return "border-l-info";
    case "tool": return "border-l-success";
    case "user":
    case "human": return "border-l-(--skill-foreground)";
    default: return "border-l-muted-foreground";
  }
}

function roleTone(role: string): BadgeTone {
  switch (role) {
    case "system": return "warning";
    case "assistant": return "info";
    case "tool": return "success";
    case "user":
    case "human": return "skill";
    default: return "neutral";
  }
}

function cacheTone(cache: ReturnType<typeof cacheUsage>, decline: number | undefined): BadgeTone {
  if (!cache) return "neutral";
  if (decline !== undefined && decline >= cacheDeclineAlertThreshold) return "destructive";
  if (decline !== undefined && decline > cacheDeclineWarningThreshold) return "warning";
  return "info";
}

type RoleFilter = "all" | "assistant" | "system" | "human" | "tool";
type SortKey = "sequence" | "created_at" | "updated_at";
type ContextNavigation = { queryKey: string; sequence: number };
type FilteredViewSnapshot = { scrollTop: number; selectedSequence: number | null };
type PendingListScroll = { kind: "sequence"; sequence: number } | { kind: "offset"; offset: number };
const cacheDeclineAlertThreshold = 5;
const cacheDeclineWarningThreshold = 2;

function Field({ name, value }: { name: string; value: unknown }) {
  return (
    <section className="min-w-0 border-b last:border-b-0">
      <h3 className="px-4 pt-3 text-2xs font-semibold uppercase tracking-widest text-muted-foreground">{name}</h3>
      <pre className="max-h-[34vh] overflow-auto whitespace-pre-wrap break-words px-4 py-3 font-mono text-xs leading-5 text-foreground">{jsonText(value)}</pre>
    </section>
  );
}

export function ContextObserverPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [taskInput, setTaskInput] = useState(searchParams.get("task_id") ?? "");
  const [runInput, setRunInput] = useState(searchParams.get("run_id") ?? "");
  const [selectedSequence, setSelectedSequence] = useState<number | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [roleFilter, setRoleFilter] = useState<RoleFilter>("all");
  const [sortKey, setSortKey] = useState<SortKey>("sequence");
  const [sortAscending, setSortAscending] = useState(true);
  const [contextNavigation, setContextNavigation] = useState<ContextNavigation | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const filteredViewSnapshotRef = useRef<FilteredViewSnapshot | null>(null);
  const pendingListScrollRef = useRef<PendingListScroll | null>(null);

  const taskId = Number(searchParams.get("task_id"));
  const runId = Number(searchParams.get("run_id"));
  const returnToCandidate = (location.state as { returnTo?: unknown } | null)?.returnTo;
  const returnTo = typeof returnToCandidate === "string"
    && returnToCandidate.startsWith("/")
    && !returnToCandidate.startsWith("//")
    ? returnToCandidate
    : null;
  const queryKey = `${Number.isInteger(taskId) && taskId > 0 ? taskId : ""}:${Number.isInteger(runId) && runId > 0 ? runId : ""}`;
  const messageQuery = useMemo<ContextMessageQuery>(() => ({
    ...(Number.isInteger(taskId) && taskId > 0 ? { taskId } : {}),
    ...(Number.isInteger(runId) && runId > 0 ? { runId } : {}),
  }), [taskId, runId]);
  const hasQuery = messageQuery.taskId !== undefined || messageQuery.runId !== undefined;
  const contextQuery = useQuery({
    queryKey: queryKeys.contextMessages(messageQuery),
    queryFn: ({ signal }) => getContextMessages(messageQuery, { signal }),
    enabled: hasQuery,
    // 上下文在 Run 执行期间持续变化，观测页每次查询都应读取最新序列，不复用缓存。
    staleTime: 0,
  });
  // 失败时清空列表（与旧行为一致），避免把上一轮结果当作本次查询结果展示。
  const messages = contextQuery.isError ? NO_MESSAGES : contextQuery.data ?? NO_MESSAGES;
  const loading = contextQuery.isFetching;
  const error = contextQuery.isError ? userErrorMessage(contextQuery.error, "上下文读取失败") : null;

  useEffect(() => {
    setTaskInput(searchParams.get("task_id") ?? "");
    setRunInput(searchParams.get("run_id") ?? "");
  }, [searchParams]);

  useEffect(() => {
    if (!contextNavigation || contextNavigation.queryKey === queryKey) return;
    setContextNavigation(null);
    filteredViewSnapshotRef.current = null;
    pendingListScrollRef.current = { kind: "offset", offset: 0 };
  }, [contextNavigation, queryKey]);

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
  const isRoleFilterNarrowingResults = visibleMessages.length < messages.length;
  const canOpenFullContext = !loading && !error && isRoleFilterNarrowingResults && visibleMessages.length > 0;
  const contextMessages = useMemo(
    () => [...messages].sort((left, right) => left.sequence - right.sequence),
    [messages],
  );
  const activeContextNavigation = contextNavigation?.queryKey === queryKey ? contextNavigation : null;
  const renderedMessages = activeContextNavigation ? contextMessages : visibleMessages;
  const sequenceRange = useMemo(() => messages.reduce<{ min: number; max: number } | null>(
    (range, message) => range
      ? { min: Math.min(range.min, message.sequence), max: Math.max(range.max, message.sequence) }
      : { min: message.sequence, max: message.sequence },
    null,
  ), [messages]);
  const selected = useMemo(
    () => renderedMessages.find((message) => message.sequence === selectedSequence) ?? null,
    [renderedMessages, selectedSequence],
  );
  useEffect(() => {
    if (renderedMessages.some((message) => message.sequence === selectedSequence)) return;
    setSelectedSequence(renderedMessages[0]?.sequence ?? null);
  }, [renderedMessages, selectedSequence]);
  const virtualizer = useVirtualizer({
    count: renderedMessages.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => 128,
    overscan: 8,
    getItemKey: (index) => {
      const message = renderedMessages[index];
      return message ? `${message.task_id}-${message.sequence}` : index;
    },
  });

  useEffect(() => {
    const pendingScroll = pendingListScrollRef.current;
    if (!pendingScroll) return;
    const frame = window.requestAnimationFrame(() => {
      if (pendingListScrollRef.current !== pendingScroll) return;
      if (pendingScroll.kind === "sequence") {
        const index = renderedMessages.findIndex((message) => message.sequence === pendingScroll.sequence);
        if (index >= 0) virtualizer.scrollToIndex(index, { align: "center" });
      } else {
        virtualizer.scrollToOffset(pendingScroll.offset);
      }
      pendingListScrollRef.current = null;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeContextNavigation?.sequence, renderedMessages, virtualizer]);

  const openFullContext = () => {
    if (activeContextNavigation || !canOpenFullContext) return;
    const targetSequence = visibleMessages.some((message) => message.sequence === selectedSequence)
      ? selectedSequence
      : visibleMessages[0]?.sequence;
    if (targetSequence === null || targetSequence === undefined) return;
    filteredViewSnapshotRef.current = {
      scrollTop: listRef.current?.scrollTop ?? 0,
      selectedSequence,
    };
    pendingListScrollRef.current = { kind: "sequence", sequence: targetSequence };
    setContextNavigation({ queryKey, sequence: targetSequence });
    setSelectedSequence(targetSequence);
  };

  const returnToFilteredMessages = () => {
    const snapshot = filteredViewSnapshotRef.current;
    filteredViewSnapshotRef.current = null;
    setContextNavigation(null);
    if (!snapshot) {
      pendingListScrollRef.current = { kind: "offset", offset: 0 };
      return;
    }
    setSelectedSequence(snapshot.selectedSequence);
    pendingListScrollRef.current = { kind: "offset", offset: snapshot.scrollTop };
  };

  const resetContextNavigation = () => {
    setContextNavigation(null);
    filteredViewSnapshotRef.current = null;
    pendingListScrollRef.current = { kind: "offset", offset: 0 };
  };

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
    resetContextNavigation();
    // 参数未变时 URL 不变、查询键不变，需要显式重新读取；参数变化由新查询键触发读取。
    if (params.toString() === new URLSearchParams(searchParams).toString()) void contextQuery.refetch();
    else setSearchParams(params);
  };

  const copySelected = async () => {
    if (!selected) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(selected, null, 2));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch (cause) {
      notify.error("复制失败", { error: new UserFacingError("请检查桌面应用的剪贴板权限。") });
      void frontendLog("WARNING", "context_message_copy_failed", "复制上下文消息 JSON 失败", {
        data: { taskId: selected.task_id, sequence: selected.sequence },
        error: cause,
      });
    }
  };

  const refresh = () => {
    resetContextNavigation();
    void contextQuery.refetch();
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <form onSubmit={submit} className="flex shrink-0 flex-wrap items-end gap-3 border-b px-5 py-4">
        {returnTo && <Button type="button" variant="ghost" size="sm" className="h-10 shrink-0 gap-1.5 px-2" onClick={() => navigate(returnTo)} aria-label="返回之前的对话" data-testid="context-observer-back"><ArrowLeftIcon aria-hidden="true" />返回对话</Button>}
        <div className="min-w-40 flex-1 basis-48">
          <label htmlFor="context-task-id" className="mb-1.5 block text-xs font-medium text-muted-foreground">Task ID</label>
          <Input id="context-task-id" inputMode="numeric" value={taskInput} onChange={(event) => setTaskInput(event.target.value)} placeholder="例如 42" />
        </div>
        <div className="min-w-40 flex-1 basis-48">
          <label htmlFor="context-run-id" className="mb-1.5 block text-xs font-medium text-muted-foreground">Run ID</label>
          <Input id="context-run-id" inputMode="numeric" value={runInput} onChange={(event) => setRunInput(event.target.value)} placeholder="例如 21" />
        </div>
        <Button type="submit" className="gap-2"><SearchIcon />查询</Button>
        <Button type="button" variant="outline" size="icon" onClick={refresh} disabled={loading || !hasQuery} aria-label="刷新上下文"><RefreshCwIcon className={loading ? "animate-spin" : ""} /></Button>
        {validationError && <p role="alert" className="basis-full text-xs text-destructive">{validationError}</p>}
      </form>

      <div className="flex min-h-0 flex-1 flex-col xl:flex-row">
        <section className="flex min-h-0 min-w-0 flex-1 flex-col xl:w-[56%] xl:border-r" aria-label="上下文消息列表">
          <div className="shrink-0 space-y-3 border-b px-5 py-3">
            <div className="flex items-baseline justify-between gap-3">
              <h2 className="text-sm font-semibold">消息序列</h2>
              <span className="shrink-0 font-mono text-2xs tabular-nums text-muted-foreground">
                {loading ? "正在读取…" : activeContextNavigation ? `${messages.length} 条完整序列` : roleFilter === "all" ? `${messages.length} 条` : `${visibleMessages.length} / ${messages.length} 条`}
                {sequenceRange && ` · #${sequenceRange.min}–${sequenceRange.max}`}
              </span>
            </div>
            {activeContextNavigation ? (
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs text-muted-foreground">当前查询范围内的全部角色消息 · 定位 #{activeContextNavigation.sequence}</p>
                <Button type="button" variant="ghost" size="sm" className="h-8 gap-1.5 px-2 text-xs text-primary hover:bg-accent" onClick={returnToFilteredMessages}><Undo2Icon aria-hidden="true" />返回筛选结果</Button>
              </div>
            ) : <div className="flex flex-wrap items-center gap-2">
              <label htmlFor="context-role-filter" className="sr-only">按角色筛选</label>
              <NativeSelect id="context-role-filter" size="sm" wrapperClassName="w-auto" value={roleFilter} onChange={(event) => setRoleFilter(event.target.value as RoleFilter)}>
                <option value="all">全部角色</option>
                <option value="assistant">AI</option>
                <option value="system">System</option>
                <option value="human">Human</option>
                <option value="tool">Tool</option>
              </NativeSelect>
              <label htmlFor="context-sort-key" className="sr-only">排序依据</label>
              <NativeSelect id="context-sort-key" size="sm" wrapperClassName="w-auto" value={sortKey} onChange={(event) => setSortKey(event.target.value as SortKey)}>
                <option value="sequence">按序号</option>
                <option value="created_at">按创建时间</option>
                <option value="updated_at">按更新时间</option>
              </NativeSelect>
              <Button type="button" variant="outline" size="sm" className="h-8 gap-1.5 px-2 text-xs" onClick={() => setSortAscending((ascending) => !ascending)} aria-label={sortAscending ? "切换为降序" : "切换为升序"}>
                {sortAscending ? <ArrowUpIcon /> : <ArrowDownIcon />}{sortAscending ? "升序" : "降序"}
              </Button>
              {canOpenFullContext && <Button type="button" variant="ghost" size="sm" className="ml-auto h-8 gap-1.5 px-2 text-xs text-primary hover:bg-accent" onClick={openFullContext}><ListTreeIcon aria-hidden="true" />查看完整上下文</Button>}
            </div>}
          </div>
          <div ref={listRef} className="min-h-0 flex-1 overflow-auto">
            {error && <ErrorState className="m-5" message={error} onRetry={refresh} />}
            {!loading && !error && messages.length === 0 && (
              <EmptyState
                className="m-5"
                title={taskId || runId ? "没有找到上下文消息" : "输入 Task ID 或 Run ID 开始查询"}
                description="结果包含流式草稿和未纳入模型上下文的记录，时间按消息创建和更新时刻展示。"
              />
            )}
            {!loading && !error && messages.length > 0 && renderedMessages.length === 0 && <EmptyState className="m-5" title="当前角色筛选下没有消息。" />}
            {renderedMessages.length > 0 && (
              <div className="relative" style={{ height: `${virtualizer.getTotalSize()}px` }}>
                {virtualizer.getVirtualItems().map((virtualRow) => {
                  const message = renderedMessages[virtualRow.index];
                  const active = message.sequence === selectedSequence;
                  const isFocusTarget = message.sequence === activeContextNavigation?.sequence;
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
                      aria-current={isFocusTarget ? "location" : undefined}
                      className={`absolute left-0 top-0 flex min-h-24 w-full flex-col gap-1 border-b border-l-2 px-4 py-2.5 text-left transition-colors ${roleStyle(message.role)} ${isFocusTarget ? "bg-primary/10 ring-2 ring-inset ring-primary/35" : active ? "bg-accent/80 ring-1 ring-inset ring-foreground/10" : message.include_in_context ? "hover:bg-muted/60" : "bg-muted/40 hover:bg-muted/60"}`}
                      style={{ transform: `translateY(${virtualRow.start}px)` }}
                    >
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="w-12 shrink-0 font-mono text-xs text-muted-foreground">#{message.sequence}</span>
                        <Badge tone={roleTone(message.role)} shape="tag" size="sm" className="font-semibold uppercase tracking-wide">{roleLabel(message.role)}</Badge>
                        <span className="truncate font-mono text-2xs text-muted-foreground">Run {message.run_id ?? "—"}</span>
                        {!message.include_in_context && <Badge variant="outline" shape="tag" size="sm" className="bg-background/70">未纳入上下文</Badge>}
                      </div>
                      <p className={`line-clamp-2 pl-12 text-xs leading-4 ${message.include_in_context ? "text-muted-foreground" : "text-muted-foreground/75"}`}>{preview(message.content) || "（空内容）"}</p>
                      {callNames.length > 0 && <div className="flex min-w-0 flex-wrap items-center gap-1 pl-12 text-2xs"><span className="shrink-0 text-muted-foreground">调用工具</span>{callNames.map((name, index) => <Badge key={`${name}-${index}`} tone="info" variant="outline" shape="tag" size="sm" className="max-w-full font-mono font-normal"><span className="truncate">{name}</span></Badge>)}</div>}
                      <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-0.5 pl-12 text-2xs font-medium tabular-nums text-foreground/75">
                        <span className="inline-flex min-w-0 items-center gap-1 truncate" title={message.created_at}><Clock3Icon className="size-3 shrink-0 text-muted-foreground" aria-hidden="true" /><span className="text-muted-foreground">创建</span><time dateTime={message.created_at}>{formatTimestamp(message.created_at, true)}</time></span><span className="inline-flex min-w-0 items-center gap-1 truncate" title={message.updated_at}><span className="text-muted-foreground">更新</span><time dateTime={message.updated_at}>{formatTimestamp(message.updated_at, true)}</time></span>
                        {message.role === "assistant" && <Badge tone={cacheTone(cache, cacheDecline)} variant="tint" shape="tag" className="font-semibold tabular-nums" title={cache ? `缓存命中 ${formatTokens(cache.hit)} / 输入 ${formatTokens(cache.input)} tokens${cacheDecline === undefined ? "" : `；较上一条可计算记录下降 ${cacheDecline.toFixed(1)} 个百分点`}` : "供应商未提供可计算缓存率的输入明细"}><GaugeIcon aria-hidden="true" />缓存 {cache ? `${cache.rate.toFixed(1)}%` : "—"}{cacheDecline !== undefined && cacheDecline > cacheDeclineWarningThreshold && <span className="ml-0.5">↓{cacheDecline.toFixed(1)} 个百分点</span>}</Badge>}
                        {toolStatus && StatusIcon && <Badge tone={toolStatus.tone} variant="tint" shape="tag" className="gap-1.5 font-semibold" title={message.status ?? undefined}><StatusIcon className={message.status === "running" ? "animate-spin" : undefined} aria-hidden="true" />{toolStatus.label}</Badge>}
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
              {selected && <p className="truncate font-mono text-2xs text-muted-foreground">Task {selected.task_id} · Run {selected.run_id ?? "—"} · sequence {selected.sequence}</p>}
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
