import { useEffect, useMemo, useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { measureRenderedElement } from "@/lib/virtual/measure-rendered-element";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  SearchIcon,
  Undo2Icon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/state-panel";
import type { ObservedMessage, ObservedRun } from "@/lib/api/session-observation";
import {
  cacheChanges,
  cacheUsage,
  contextState,
  formatTime,
  INITIAL_FILTERS,
  kindLabel,
  MESSAGE_ROLES,
  messagePreview,
  messageTitle,
  messageTools,
  toolOptions,
  toolStatus,
  type MessageFilters,
} from "./message-model";
import { mergeTimeline, nodeNotes, nodeTitle, nodeTone, runNodes, type TimelineItem } from "./run-nodes";
import type { MessageNavigation } from "./use-message-navigation";

const NODE_TONE_CLASS = {
  neutral: "text-muted-foreground",
  warning: "text-warning",
  destructive: "text-destructive",
} as const;

export function MessageList({
  messages,
  runs,
  navigation: nav,
  visible,
}: {
  messages: ObservedMessage[];
  runs: ObservedRun[];
  navigation: MessageNavigation;
  visible: boolean;
}) {
  const host = useRef<HTMLDivElement>(null);
  const changes = useMemo(() => cacheChanges(messages), [messages]);
  const nodes = useMemo(() => runNodes(runs), [runs]);
  const toolChoices = useMemo(() => toolOptions(messages), [messages]);
  // 节点解释的是时间上的先后：只在列表按时间顺序完整展示时插入；按更新时间排序、搜索或筛选出的
  // 子集里，行之间已不相邻，插进去会误读。
  const { filters } = nav;
  const timelineOrdered =
    nav.reveal ||
    (filters.sort !== "updated_at" && !filters.search.trim() && filters.role === "all" && filters.tool === "all" && filters.inclusion === "all");
  const items = useMemo<TimelineItem[]>(
    () =>
      timelineOrdered
        ? mergeTimeline(nav.rows, nodes, nav.reveal || filters.ascending)
        : nav.rows.map((message) => ({ type: "message", message })),
    [filters.ascending, nav.reveal, nav.rows, nodes, timelineOrdered],
  );
  const indexOf = (sequence: number | undefined) =>
    items.findIndex((item) => item.type === "message" && item.message.sequence === sequence);
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => host.current,
    estimateSize: (index) => (items[index].type === "node" ? 36 : 60),
    overscan: 6,
    getItemKey: (index) => {
      const item = items[index];
      return item.type === "node" ? item.node.key : item.message.sequence;
    },
    // 窄窗口打开详情时列表被隐藏，隐藏期间的 0 高度不能进缓存，否则返回列表时整体下跳。
    measureElement: measureRenderedElement,
  });
  useEffect(() => {
    if (!visible || !nav.scroll || !nav.rows.length) return;
    const intent = nav.scroll;
    const frame = requestAnimationFrame(() => {
      if ("sequence" in intent) {
        const index = indexOf(intent.sequence);
        if (index >= 0) virtualizer.scrollToIndex(index, { align: "center" });
      } else virtualizer.scrollToOffset(intent.offset);
      nav.clearScroll();
    });
    return () => cancelAnimationFrame(frame);
    // 滚动命令只在可见列表中消费；刷新消息不会重新发出定位命令。
  }, [nav.scroll, nav.rows, items, visible, virtualizer]);
  const selectedIndex = nav.rows.findIndex(
    (row) => row.sequence === nav.selected?.sequence,
  );
  const selectedItemIndex = indexOf(nav.selected?.sequence);
  const canLocate = !nav.reveal && nav.rows.length < messages.length && nav.selected !== null;
  // 按单个回合查看时每行回合号都一样，只在跨回合的范围里标出来（系统提示词行不算回合）。
  const mixedTurns = useMemo(
    () => new Set(messages.filter((m) => m.sequence > 0).map((m) => m.turn)).size > 1,
    [messages],
  );
  return (
    <section
      className="flex h-full min-h-0 flex-col"
      aria-label="会话消息列表"
    >
      <div className="shrink-0 space-y-2.5 border-b px-4 py-3">
        <div className="flex h-7 items-center gap-2">
          <h2 className="text-sm font-semibold">消息序列</h2>
          {canLocate && (
            <Button
              size="sm"
              variant="ghost"
              className="ml-auto text-xs"
              onClick={() => nav.locate(nav.selected!.sequence)}
            >
              在全部记录中定位
            </Button>
          )}
          <span className={`text-xs tabular-nums text-muted-foreground ${canLocate ? "" : "ml-auto"}`}>
            {nav.rows.length} / {messages.length} 条
          </span>
        </div>
        {nav.reveal ? (
          <div className="space-y-1 text-xs">
            <p className="text-muted-foreground">
              当前范围的全部记录 · 定位 #{nav.selected?.sequence}
            </p>
            <Button size="sm" variant="ghost" onClick={nav.returnToResults}>
              <Undo2Icon />
              返回筛选结果
            </Button>
          </div>
        ) : (
          <>
            <div className="relative">
              <SearchIcon
                className="pointer-events-none absolute left-2.5 top-2 size-4 text-muted-foreground"
                aria-hidden="true"
              />
              <Input
                className="pl-8 text-xs"
                aria-label="搜索已加载消息"
                placeholder="搜索消息、工具名、序号…"
                value={nav.filters.search}
                onChange={(event) =>
                  nav.updateFilters({ search: event.target.value })
                }
              />
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <NativeSelect
                size="sm"
                wrapperClassName="w-auto"
                aria-label="按角色筛选"
                value={nav.filters.role}
                onChange={(event) =>
                  nav.updateFilters({ role: event.target.value })
                }
              >
                <option value="all">全部角色</option>
                {MESSAGE_ROLES.map((role) => (
                  <option key={role} value={role}>
                    {role}
                  </option>
                ))}
              </NativeSelect>
              {toolChoices.length > 0 && (
                <NativeSelect
                  size="sm"
                  wrapperClassName="w-auto min-w-24 max-w-48 flex-1"
                  className={nav.filters.tool === "all" ? undefined : "border-primary text-primary"}
                  aria-label="按工具筛选"
                  value={nav.filters.tool}
                  onChange={(event) =>
                    nav.updateFilters({ tool: event.target.value })
                  }
                >
                  <option value="all">全部工具</option>
                  {toolChoices.map(({ name, calls }) => (
                    <option key={name} value={name}>
                      {name}（{calls} 次）
                    </option>
                  ))}
                </NativeSelect>
              )}
              <NativeSelect
                size="sm"
                wrapperClassName="w-auto"
                aria-label="按纳入范围筛选"
                value={nav.filters.inclusion}
                onChange={(event) =>
                  nav.updateFilters({
                    inclusion: event.target
                      .value as MessageFilters["inclusion"],
                  })
                }
              >
                <option value="all">全部记录</option>
                <option value="included">进了上下文</option>
                <option value="excluded">没进上下文</option>
              </NativeSelect>
              {/* 排序是低频操作，与筛选分开靠右，依据和方向并成一组。 */}
              <div className="ml-auto flex items-center">
              <NativeSelect
                size="sm"
                wrapperClassName="w-auto"
                className="border-transparent shadow-none hover:border-input"
                aria-label="排序依据"
                value={nav.filters.sort}
                onChange={(event) =>
                  nav.updateFilters({
                    sort: event.target.value as MessageFilters["sort"],
                  })
                }
              >
                <option value="sequence">序号</option>
                <option value="created_at">创建时间</option>
                <option value="updated_at">更新时间</option>
              </NativeSelect>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={nav.filters.ascending ? "切换为降序" : "切换为升序"}
                onClick={() =>
                  nav.updateFilters({ ascending: !nav.filters.ascending })
                }
              >
                {nav.filters.ascending ? <ArrowUpIcon /> : <ArrowDownIcon />}
              </Button>
              </div>
            </div>
          </>
        )}
      </div>
      <div
        ref={host}
        className="min-h-0 flex-1 overflow-auto focus-ring"
        tabIndex={0}
        role="listbox"
        aria-label="消息序列"
        aria-activedescendant={
          virtualizer
            .getVirtualItems()
            .some((row) => row.index === selectedItemIndex) && nav.selected
            ? `observer-message-${nav.selected.sequence}`
            : undefined
        }
        onScroll={(event) => {
          nav.listOffset.current = event.currentTarget.scrollTop;
        }}
        onKeyDown={(event) => {
          if (
            !["ArrowDown", "ArrowUp", "Home", "End", "Enter"].includes(
              event.key,
            ) ||
            !nav.rows.length
          )
            return;
          event.preventDefault();
          if (event.key === "Enter") {
            nav.setDetailOpen(true);
            return;
          }
          const index =
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? nav.rows.length - 1
                : Math.max(
                    0,
                    Math.min(
                      nav.rows.length - 1,
                      selectedIndex + (event.key === "ArrowDown" ? 1 : -1),
                    ),
                  );
          nav.select(nav.rows[index].sequence, true);
        }}
      >
        {!nav.rows.length && (
          <EmptyState
            className="m-4 border-0"
            title="当前筛选没有匹配的消息"
            description="搜索仅覆盖当前查询范围内已加载的记录。"
            action={
              <Button
                variant="outline"
                size="sm"
                onClick={() => nav.updateFilters(INITIAL_FILTERS)}
              >
                清除筛选
              </Button>
            }
          />
        )}
        <div
          className="relative"
          style={{ height: virtualizer.getTotalSize() }}
        >
          {virtualizer.getVirtualItems().map((row) => {
            const item = items[row.index];
            if (item.type === "node") {
              const { change } = item.node;
              const notes = nodeNotes(item.node);
              return (
                <div
                  key={item.node.key}
                  ref={virtualizer.measureElement}
                  data-index={row.index}
                  role="separator"
                  aria-label={`运行节点：${nodeTitle(change)}`}
                  data-testid="observer-run-node"
                  className="absolute left-0 top-0 flex w-full flex-wrap items-center gap-x-2 gap-y-0.5 border-b bg-muted/40 px-4 py-2 text-xs tabular-nums text-muted-foreground"
                  style={{ transform: `translateY(${row.start}px)` }}
                >
                  <span className={`font-medium ${NODE_TONE_CLASS[nodeTone(change)]}`}>
                    {nodeTitle(change)}
                  </span>
                  {notes.map((note) => (
                    <span key={note}>· {note}</span>
                  ))}
                  <time className="ml-auto" dateTime={change.at} title={formatTime(change.at, true)}>
                    {formatTime(change.at)}
                  </time>
                </div>
              );
            }
            const { message } = item;
            const active = message.sequence === nav.selected?.sequence;
            const cache =
              message.role === "assistant" ? cacheUsage(message) : null;
            const change = changes.get(message.sequence);
            const context = contextState(message);
            const tools = messageTools(message);
            const failed = message.role === "tool" && message.status === "failed";
            const timeField = nav.filters.sort === "updated_at" ? "updated_at" : "created_at";
            // 行上只标例外：正常完成、进了上下文、在对话里显示是常态，不占位置；详情里仍有全部字段。
            return (
              <div
                key={message.sequence}
                id={`observer-message-${message.sequence}`}
                ref={virtualizer.measureElement}
                data-index={row.index}
                role="option"
                aria-selected={active}
                aria-label={`#${message.sequence} ${messageTitle(message)}`}
                data-testid={`observer-row-${message.sequence}`}
                className={`absolute left-0 top-0 w-full cursor-pointer border-b border-l-[3px] px-4 py-2 ${active ? "border-l-primary bg-accent" : `${failed ? "border-l-destructive" : "border-l-transparent"} hover:bg-muted/50`}`}
                style={{ transform: `translateY(${row.start}px)` }}
                onClick={() => {
                  nav.select(message.sequence);
                  nav.setDetailOpen(true);
                  host.current?.focus({ preventScroll: true });
                }}
              >
                <div className="flex items-center gap-2 text-xs">
                  <span className="shrink-0 font-mono text-muted-foreground">
                    #{message.sequence}
                  </span>
                  <span className="shrink-0 font-semibold">{kindLabel(message)}</span>
                  {tools.length > 0 && (
                    <span className="min-w-0 truncate font-mono text-muted-foreground">
                      {tools.join("、")}
                    </span>
                  )}
                  {message.role === "tool" && message.status !== "completed" && (
                    <Badge
                      shape="tag"
                      size="sm"
                      className="shrink-0"
                      tone={failed ? "destructive" : message.status === "cancelled" ? "warning" : "neutral"}
                    >
                      {toolStatus(message.status)}
                    </Badge>
                  )}
                  {!context.included && (
                    <Badge shape="tag" size="sm" variant="outline" tone="warning" className="shrink-0">
                      {context.label}
                    </Badge>
                  )}
                  {!message.project_to_transport && !message.withdrawn && (
                    <Badge shape="tag" size="sm" variant="outline" tone="neutral" className="shrink-0">
                      对话中不显示
                    </Badge>
                  )}
                  <span className="ml-auto flex shrink-0 gap-2 tabular-nums text-muted-foreground">
                    {cache && (
                      <span title="相较同一上下文范围内上一条可计算记录；变化不代表故障">
                        缓存 {cache.rate}%
                        {change !== undefined && change !== 0
                          ? ` ${change > 0 ? "↑" : "↓"}${Math.abs(change)}`
                          : ""}
                      </span>
                    )}
                    {message.inherited && <span>继承</span>}
                    {mixedTurns && message.sequence > 0 && <span>回合 {message.turn}</span>}
                    <time dateTime={message[timeField]} title={formatTime(message[timeField], true)}>
                      {timeField === "updated_at" ? "更新 " : ""}
                      {formatTime(message[timeField])}
                    </time>
                  </span>
                </div>
                <p className="mt-1 truncate text-sm leading-5">
                  {messagePreview(message)}
                </p>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
