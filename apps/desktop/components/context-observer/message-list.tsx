import { useEffect, useMemo, useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
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
import type { ContextMessage } from "@/lib/api/context-messages";
import {
  cacheChanges,
  cacheUsage,
  formatTime,
  INITIAL_FILTERS,
  messagePreview,
  roleLabel,
  toolStatus,
  type MessageFilters,
} from "./message-model";
import type { MessageNavigation } from "./use-message-navigation";

export function MessageList({
  messages,
  navigation: nav,
  visible,
}: {
  messages: ContextMessage[];
  navigation: MessageNavigation;
  visible: boolean;
}) {
  const host = useRef<HTMLDivElement>(null);
  const jumpInput = useRef<HTMLInputElement>(null);
  const changes = useMemo(() => cacheChanges(messages), [messages]);
  const virtualizer = useVirtualizer({
    count: nav.rows.length,
    getScrollElement: () => host.current,
    estimateSize: () => 104,
    overscan: 6,
    getItemKey: (index) => nav.rows[index].sequence,
  });
  useEffect(() => {
    if (!visible || !nav.scroll || !nav.rows.length) return;
    const intent = nav.scroll;
    const frame = requestAnimationFrame(() => {
      if ("sequence" in intent) {
        const index = nav.rows.findIndex(
          (row) => row.sequence === intent.sequence,
        );
        if (index >= 0) virtualizer.scrollToIndex(index, { align: "center" });
      } else virtualizer.scrollToOffset(intent.offset);
      nav.clearScroll();
    });
    return () => cancelAnimationFrame(frame);
    // 滚动命令只在可见列表中消费；刷新消息不会重新发出定位命令。
  }, [nav.scroll, nav.rows, visible, virtualizer]);
  const selectedIndex = nav.rows.findIndex(
    (row) => row.sequence === nav.selected?.sequence,
  );
  return (
    <section
      className="flex h-full min-h-0 flex-col"
      aria-label="上下文消息列表"
    >
      <div className="shrink-0 space-y-3 border-b px-4 py-3">
        <div className="flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold">消息序列</h2>
          <span className="text-xs tabular-nums text-muted-foreground">
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
            <div className="flex flex-wrap gap-2">
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
                <option value="system">System</option>
                <option value="user">User</option>
                <option value="assistant">Assistant</option>
                <option value="tool">Tool</option>
              </NativeSelect>
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
                <option value="included">已纳入上下文</option>
                <option value="excluded">未纳入上下文</option>
              </NativeSelect>
              <NativeSelect
                size="sm"
                wrapperClassName="w-auto"
                aria-label="排序依据"
                value={nav.filters.sort}
                onChange={(event) =>
                  nav.updateFilters({
                    sort: event.target.value as MessageFilters["sort"],
                  })
                }
              >
                <option value="sequence">按序号</option>
                <option value="created_at">按创建时间</option>
                <option value="updated_at">按更新时间</option>
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
          </>
        )}
        <form
          className="flex items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            const input = jumpInput.current!;
            const sequence = Number(input.value);
            if (
              !/^\d+$/.test(input.value) ||
              !messages.some((message) => message.sequence === sequence)
            ) {
              input.setCustomValidity("当前查询范围没有该序号。");
              input.reportValidity();
              return;
            }
            input.setCustomValidity("");
            nav.locate(sequence);
          }}
        >
          <Input
            ref={jumpInput}
            className="w-24 text-xs"
            aria-label="定位序号"
            inputMode="numeric"
            placeholder="序号 #"
            onChange={(event) => event.target.setCustomValidity("")}
          />
          <Button size="sm" variant="ghost" type="submit">
            定位
          </Button>
          {!nav.reveal && nav.rows.length < messages.length && nav.selected && (
            <Button
              size="sm"
              variant="ghost"
              className="ml-auto text-xs"
              onClick={() => nav.locate(nav.selected!.sequence)}
            >
              在全部记录中定位
            </Button>
          )}
        </form>
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
            .some((row) => row.index === selectedIndex) && nav.selected
            ? `context-message-${nav.selected.sequence}`
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
            const message = nav.rows[row.index];
            const active = message.sequence === nav.selected?.sequence;
            const cache =
              message.role === "assistant" ? cacheUsage(message) : null;
            const change = changes.get(message.sequence);
            return (
              <div
                key={message.sequence}
                id={`context-message-${message.sequence}`}
                ref={virtualizer.measureElement}
                data-index={row.index}
                role="option"
                aria-selected={active}
                aria-label={`#${message.sequence} ${roleLabel(message.role)}`}
                data-testid={`context-row-${message.sequence}`}
                className={`absolute left-0 top-0 w-full cursor-pointer border-b border-l px-4 py-3 ${active ? "border-l-primary bg-accent" : "border-l-transparent hover:bg-muted/50"}`}
                style={{ transform: `translateY(${row.start}px)` }}
                onClick={() => {
                  nav.select(message.sequence);
                  nav.setDetailOpen(true);
                  host.current?.focus({ preventScroll: true });
                }}
              >
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="font-mono text-muted-foreground">
                    #{message.sequence}
                  </span>
                  <span className="font-semibold">
                    {roleLabel(message.role)}
                  </span>
                  {message.role === "tool" && (
                    <Badge
                      shape="tag"
                      size="sm"
                      tone={
                        message.status === "failed"
                          ? "destructive"
                          : message.status === "cancelled"
                            ? "warning"
                            : "neutral"
                      }
                    >
                      {toolStatus(message.status)}
                    </Badge>
                  )}
                  {!message.include_in_context && (
                    <span className="ml-auto text-xs text-muted-foreground">
                      未纳入
                    </span>
                  )}
                </div>
                <p className="mt-1.5 line-clamp-2 break-words text-sm leading-5">
                  {messagePreview(message)}
                </p>
                <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs tabular-nums text-muted-foreground">
                  <time
                    dateTime={
                      message[
                        nav.filters.sort === "updated_at"
                          ? "updated_at"
                          : "created_at"
                      ]
                    }
                    title={formatTime(
                      message[
                        nav.filters.sort === "updated_at"
                          ? "updated_at"
                          : "created_at"
                      ],
                      true,
                    )}
                  >
                    {nav.filters.sort === "updated_at" ? "更新" : "创建"}{" "}
                    {formatTime(
                      message[
                        nav.filters.sort === "updated_at"
                          ? "updated_at"
                          : "created_at"
                      ],
                    )}
                  </time>
                  <span>
                    {message.run_id === null
                      ? "任务系统提示词"
                      : `Run #${message.run_id}`}
                  </span>
                  {cache && (
                    <span title="相较同一 Run 内上一条可计算记录；变化不代表故障">
                      缓存 {cache.rate}%
                      {change !== undefined && change !== 0
                        ? ` · ${change > 0 ? "+" : ""}${change} 个百分点`
                        : ""}
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
      <p className="shrink-0 border-t px-4 py-2 text-xs text-muted-foreground">
        上下键浏览 · Enter 查看详情 · 搜索限当前范围
      </p>
    </section>
  );
}
