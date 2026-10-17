import { useChangeBrowserVisibility } from "@/lib/workspace-files/visibility";
import { IgnoreMenuItem, openKeyboardContextMenu } from "./ignore-menu-item";
import { ContextMenu } from "radix-ui";
import { useReferenceBridge } from "@/components/workspace-references/reference-bridge";
import { AddReferenceButton } from "@/components/workspace-references/add-reference-button";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { useEffect, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  readFileStream,
  parseSearchBatch,
  SearchBatchContractError,
  type FileEntry,
  type SearchBatch,
} from "@/lib/api/workspace-files";
import { WorkspaceFileIcon } from "./file-icon";
import { Button } from "@/components/ui/button";
import { frontendLog } from "@/lib/logging/frontend-log";

export function FileSearch({
  workspaceId,
  query,
  open,
  clear,
  onManageIgnored,
}: {
  workspaceId: number;
  query: string;
  open: (path: string) => void;
  clear: () => void;
  onManageIgnored: () => void;
}) {
  const { change, pending } = useChangeBrowserVisibility(workspaceId);
  const bridge = useReferenceBridge();
  const [selected, setSelected] = useState<string[]>([]);
  const anchor = useRef(0);
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [status, setStatus] = useState("正在搜索…");
  const [phase, setPhase] = useState<
    "running" | "complete" | "stopped" | "failed"
  >("running");
  const [reasons, setReasons] = useState<Record<string, number>>({});
  const [skipped, setSkipped] = useState(0);
  const [offset, setOffset] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  useEffect(() => setOffset(0), [query]);
  const [retry, setRetry] = useState(0);
  const control = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    control.current = controller;
    setEntries([]);
    setSelected([]);
    setSkipped(0);
    setReasons({});
    setPhase("running");
    setNextOffset(null);
    setStatus("正在搜索…");
    void readFileStream<SearchBatch>(
      workspaceId,
      "search",
      {
        query,
        offset,
      },
      controller.signal,
      (batch) => {
        if (controller.signal.aborted) return;
        if (batch.error) throw new Error(batch.error);
        setEntries((previous) => [...previous, ...batch.entries]);
        setSkipped(batch.skipped);
        setReasons(batch.skipped_reasons);
        if (batch.complete) {
          setStatus("搜索完成");
          setPhase("complete");
          setNextOffset(batch.next_offset ?? null);
        }
      },
      parseSearchBatch,
    ).catch((error) => {
      if (!controller.signal.aborted) {
        setPhase("failed");
        setStatus(userErrorMessage(error, "搜索失败"));
        if (error instanceof SearchBatchContractError)
          void frontendLog(
            "ERROR",
            "workspace_file_search_contract_invalid",
            "文件名搜索响应不符合批次契约",
            {
              data: { workspaceId, invalidFields: error.invalidFields },
              error,
            },
          );
      }
    });
    return () => {
      controller.abort();
    };
  }, [workspaceId, query, retry, offset]);
  const host = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: entries.length,
    getScrollElement: () => host.current,
    estimateSize: () => 48,
    overscan: 5,
  });
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 p-2 text-xs">
        {selected.length > 0 && (
          <AddReferenceButton
            workspaceId={workspaceId}
            inputs={selected.map((path) => ({ path, kind: "file" }))}
            label={`添加所选 ${selected.length} 项`}
          />
        )}

        <span role="status">
          {status} ·{" "}
          {entries.length
            ? `第 ${offset + 1}–${offset + entries.length} 个文件`
            : "0 个文件"}
          {skipped > 0 ? ` · ${skipped} 个条目未扫描，结果可能不完整` : ""}
        </span>
        {phase === "running" && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              control.current?.abort();
              setPhase("stopped");
              setStatus("已停止");
            }}
          >
            停止
          </Button>
        )}
        {(phase === "stopped" || phase === "failed") && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setRetry((v) => v + 1)}
          >
            重新搜索
          </Button>
        )}
        {phase === "complete" && (offset > 0 || nextOffset !== null) && (
          <>
            <Button
              size="sm"
              variant="ghost"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - 1000))}
            >
              上一页
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={nextOffset === null}
              onClick={() => setOffset(nextOffset!)}
            >
              下一页
            </Button>
          </>
        )}
      </div>
      {skipped > 0 && (
        <details className="px-2 pb-2 text-xs text-muted-foreground">
          <summary className="cursor-pointer">查看未扫描原因</summary>
          <ul className="space-y-1 py-2">
            {Object.entries(reasons).map(([reason, count]) => (
              <li key={reason}>
                {reason}：{count} 项
              </li>
            ))}
          </ul>
          <p>
            检查目录权限或链接目标后，可使用顶部刷新重新读取目录，或重新搜索。
          </p>
          <Button
            size="sm"
            variant="ghost"
            disabled={phase === "running"}
            onClick={() => setRetry((v) => v + 1)}
          >
            重新搜索
          </Button>
        </details>
      )}
      {phase === "complete" && entries.length === 0 && (
        <div className="space-y-2 p-4 text-sm">
          <p>没有匹配的文件</p>
          <p className="text-xs text-muted-foreground">
            尝试更短的文件名或路径，或使用 glob 模式。
          </p>
          <Button size="sm" variant="outline" onClick={clear}>
            清空搜索，返回目录
          </Button>
          <Button size="sm" variant="ghost" onClick={onManageIgnored}>
            管理忽略项
          </Button>
        </div>
      )}
      <div ref={host} className="min-h-0 flex-1 overflow-auto">
        <div
          style={{ height: virtualizer.getTotalSize(), position: "relative" }}
        >
          {virtualizer.getVirtualItems().map((item) => {
            const entry = entries[item.index];
            return (
              <ContextMenu.Root key={entry.path}>
                <ContextMenu.Trigger asChild>
                  <button
                    className={`absolute left-0 flex w-full items-center gap-2 px-3 text-left text-xs hover:bg-muted ${selected.includes(entry.path) ? "bg-primary/10" : ""}`}
                    style={{ top: item.start, height: item.size }}
                    aria-pressed={selected.includes(entry.path)}
                    onKeyDown={(event) => openKeyboardContextMenu(event)}
                    onClick={(event) => {
                      if (event.shiftKey)
                        setSelected(
                          entries
                            .slice(
                              Math.min(anchor.current, item.index),
                              Math.max(anchor.current, item.index) + 1,
                            )
                            .map((entry) => entry.path),
                        );
                      else if (event.metaKey || event.ctrlKey) {
                        anchor.current = item.index;
                        setSelected((current) =>
                          current.includes(entry.path)
                            ? current.filter((path) => path !== entry.path)
                            : [...current, entry.path],
                        );
                      } else {
                        anchor.current = item.index;
                        open(entry.path);
                      }
                    }}
                    onContextMenu={() => {
                      anchor.current = item.index;
                      setSelected((current) =>
                        current.includes(entry.path) ? current : [entry.path],
                      );
                    }}
                    title={entry.path}
                  >
                    <WorkspaceFileIcon name={entry.name} />
                    <span className="min-w-0">
                      <span className="block truncate text-sm">
                        {entry.name}
                      </span>
                      <span className="block truncate text-muted-foreground">
                        {entry.path}
                      </span>
                    </span>
                  </button>
                </ContextMenu.Trigger>
                <ContextMenu.Portal>
                  <ContextMenu.Content className="z-50 min-w-40 rounded-md border bg-popover p-1 text-sm shadow-md">
                    <ContextMenu.Item
                      disabled={!bridge?.target}
                      className="cursor-default rounded px-2 py-1.5 outline-none focus:bg-muted data-[disabled]:opacity-50"
                      onSelect={() =>
                        void bridge?.add(
                          workspaceId,
                          (selected.includes(entry.path)
                            ? selected
                            : [entry.path]
                          ).map((path) => ({ path, kind: "file" })),
                        )
                      }
                    >
                      添加到对话
                    </ContextMenu.Item>
                    <IgnoreMenuItem
                      paths={
                        selected.includes(entry.path) ? selected : [entry.path]
                      }
                      pending={pending}
                      onIgnore={(paths) => {
                        const searchInput = host.current
                          ?.closest('[data-testid="workspace-files-surface"]')
                          ?.querySelector<HTMLInputElement>(
                            'input[aria-label="搜索工作区文件名"]',
                          );
                        void change(paths, true).then((ok) => {
                          if (ok) searchInput?.focus();
                        });
                      }}
                    />
                  </ContextMenu.Content>
                </ContextMenu.Portal>
              </ContextMenu.Root>
            );
          })}
        </div>
      </div>
    </div>
  );
}
