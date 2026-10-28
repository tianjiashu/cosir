import { useChangeBrowserVisibility } from "@/lib/workspace-files/visibility";
import { IgnoreMenuItem, openKeyboardContextMenu } from "./ignore-menu-item";
import { useReferenceBridge } from "@/components/workspace-references/reference-bridge";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useCallback,
} from "react";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import { Tree, type TreeApi, type NodeRendererProps } from "react-arborist";
import { ContextMenu } from "radix-ui";
import { ChevronDownIcon, ChevronRightIcon, LinkIcon } from "lucide-react";
import {
  getDirectory,
  type DirectoryPage,
  type FileEntry,
} from "@/lib/api/workspace-files";
import { queryKeys } from "@/lib/query/query-keys";
import { HttpError } from "@/lib/http/errors";
import {
  fileSession,
  parentsOf,
  useFilesStore,
} from "@/lib/workspace-files/store";
import { WorkspaceFileIcon } from "./file-icon";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS } from "@/components/ui/menu-tokens";

type TreeItem = {
  id: string;
  entry: FileEntry;
  children: TreeItem[] | null;
  more?: boolean;
  error?: string;
  parent?: string;
  ignoredEmpty?: boolean;
};
const idOf = (path: string) => `file:${path}`;

export function FileTree({
  workspaceId,
  rootName,
  rootPath,
  reveal,
  revision,
  onManageIgnored,
}: {
  workspaceId: number;
  rootName: string;
  rootPath: string;
  reveal: number;
  revision: string;
  onManageIgnored: () => void;
}) {
  const { change, pending } = useChangeBrowserVisibility(workspaceId);
  const pendingFocus = useRef<string | null>(null);
  const bridge = useReferenceBridge();
  const session = useFilesStore((s) => fileSession(s.sessions, workspaceId));
  const patch = useFilesStore((s) => s.patch);
  const open = useFilesStore((s) => s.open);
  const queryClient = useQueryClient();
  const expanded = session.expanded;
  const activeDirectories = expanded.filter((path) =>
    parentsOf(path).every((parent) => expanded.includes(parent)),
  );
  const results = useQueries({
    queries: activeDirectories.map((path) => ({
      queryKey: queryKeys.fileDirectory(workspaceId, path, revision),
      queryFn: async ({ signal }: { signal: AbortSignal }) => {
        const previous = queryClient.getQueryData<DirectoryPage>(
          queryKeys.fileDirectory(workspaceId, path, revision),
        );
        let page = await getDirectory(workspaceId, path, undefined, signal);
        while (
          page.next_cursor &&
          page.entries.length < (previous?.entries.length ?? 0)
        ) {
          const next = await getDirectory(
            workspaceId,
            path,
            page.next_cursor,
            signal,
          );
          page = { ...next, entries: [...page.entries, ...next.entries] };
        }
        return page;
      },
      retry: (count: number, error: Error) =>
        count < 1 &&
        (!(error instanceof HttpError) ||
          error.status === 409 ||
          error.status >= 500),
      gcTime: 120_000,
    })),
  });
  const loadingMore = useRef(new Map<string, AbortController>());
  useEffect(() => {
    const pending = loadingMore.current;
    return () => {
      for (const controller of pending.values()) controller.abort();
      pending.clear();
    };
  }, [workspaceId, revision]);
  const [pageErrors, setPageErrors] = useState<Record<string, string>>({});
  const loadMore = useCallback(
    async (path: string) => {
      const key = queryKeys.fileDirectory(workspaceId, path, revision);
      const current = queryClient.getQueryData<DirectoryPage>(key);
      if (!current?.next_cursor || loadingMore.current.has(path)) return;
      const controller = new AbortController();
      loadingMore.current.set(path, controller);
      try {
        const next = await getDirectory(
          workspaceId,
          path,
          current.next_cursor,
          controller.signal,
        );
        queryClient.setQueryData<DirectoryPage>(key, (value) =>
          value === current
            ? { ...next, entries: [...current.entries, ...next.entries] }
            : value,
        );
        setPageErrors((previous) => {
          const errors = { ...previous };
          delete errors[path];
          return errors;
        });
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof HttpError && error.status === 409)
          await queryClient.invalidateQueries({ queryKey: key });
        else
          setPageErrors((previous) => ({
            ...previous,
            [path]: userErrorMessage(error, "加载失败"),
          }));
      } finally {
        loadingMore.current.delete(path);
      }
    },
    [queryClient, workspaceId, revision],
  );
  const byPath = new Map(
    activeDirectories.map((path, i) => [path, results[i]]),
  );
  const build = (entry: FileEntry): TreeItem => {
    if (entry.kind !== "directory")
      return { id: idOf(entry.path), entry, children: null };
    const query = byPath.get(entry.path);
    const children = query?.data?.entries.map(build) ?? [];
    if (expanded.includes(entry.path) && !query?.data)
      children.push({
        id: `loading:${entry.path}`,
        entry: {
          ...entry,
          name: query?.error
            ? userErrorMessage(query.error, "目录加载失败")
            : "加载中…",
        },
        children: null,
        parent: entry.path,
        error: query?.error
          ? userErrorMessage(query.error, "目录加载失败")
          : undefined,
      });
    if (
      expanded.includes(entry.path) &&
      query?.data?.total === 0 &&
      query.data.hidden_count > 0
    )
      children.push({
        id: `ignored-empty:${entry.path}`,
        entry: { ...entry, name: "此目录中的内容已被忽略" },
        children: null,
        parent: entry.path,
        ignoredEmpty: true,
      });
    if (query?.data?.next_cursor)
      children.push({
        id: `more:${entry.path}`,
        entry: { ...entry, name: pageErrors[entry.path] ?? "加载更多…" },
        children: null,
        more: true,
        parent: entry.path,
        error: pageErrors[entry.path],
      });
    return { id: idOf(entry.path), entry, children };
  };
  const model = [
    build({
      path: "",
      name: rootName,
      kind: "directory",
      symlink: false,
      hint: null,
    }),
  ];
  const tree = useRef<TreeApi<TreeItem> | undefined>(undefined);
  const container = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState(0);
  useLayoutEffect(() => {
    const observer = new ResizeObserver((entries) =>
      setHeight(entries[0].contentRect.height),
    );
    if (container.current) observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  const openState = useMemo(
    () => Object.fromEntries(expanded.map((path) => [idOf(path), true])),
    [expanded],
  );
  useEffect(() => {
    for (const node of tree.current?.visibleNodes ?? []) {
      if (
        node.data.entry.kind !== "directory" ||
        node.data.parent !== undefined
      )
        continue;
      const shouldOpen = expanded.includes(node.data.entry.path);
      if (node.isOpen !== shouldOpen) {
        if (shouldOpen) node.open();
        else node.close();
      }
    }
  }, [expanded, results]);
  useEffect(() => {
    if (pendingFocus.current === null) return;
    const target = tree.current?.get(idOf(pendingFocus.current));
    if (target) {
      target.focus();
      pendingFocus.current = null;
    }
  }, [revision, results]);
  const pendingReveal = useRef(false);
  const lastReveal = useRef(0);
  useEffect(() => {
    if (reveal !== lastReveal.current) {
      pendingReveal.current = true;
      lastReveal.current = reveal;
    }
    if (!session.active || !pendingReveal.current) return;
    const path = session.active;
    const parents = parentsOf(path);
    if (parents.some((parent) => !expanded.includes(parent))) {
      patch(workspaceId, { expanded: [...new Set([...expanded, ...parents])] });
      return;
    }
    if (tree.current?.get(idOf(path))) {
      tree.current.scrollTo(idOf(path));
      pendingReveal.current = false;
      return;
    }
    for (const parent of parents) {
      const data = byPath.get(parent)?.data;
      if (data?.next_cursor) void loadMore(parent);
    }
  });
  return (
    <div
      ref={container}
      className="flex min-h-0 flex-1 flex-col"
      onKeyDown={(event) => {
        if (
          event.key === "ContextMenu" ||
          (event.shiftKey && event.key === "F10")
        ) {
          const row = tree.current?.focusedNode;
          if (!row) return;
          const trigger = container.current?.querySelector<HTMLElement>(
            `[data-file-path="${CSS.escape(row.data.entry.path)}"]`,
          );
          if (!trigger) return;
          openKeyboardContextMenu(event, trigger);
        }
      }}
      data-testid="workspace-file-tree"
    >
      {height > 0 && (
        <Tree<TreeItem>
          ref={tree}
          dndRootElement={container.current}
          data={model}
          width="100%"
          height={height}
          rowHeight={26}
          indent={16}
          openByDefault={false}
          initialOpenState={openState}
          disableDrag
          disableDrop
          disableEdit
          aria-label="工作区文件目录"
          onToggle={(id) => {
            const node = tree.current?.get(id);
            if (!node) return;
            const path = node.data.entry.path;
            const next = new Set(
              useFilesStore.getState().sessions[workspaceId]?.expanded ?? [""],
            );
            if (node.isOpen) next.add(path);
            else next.delete(path);
            patch(workspaceId, { expanded: [...next] });
          }}
          onActivate={(node) => {
            if (node.data.entry.kind === "file")
              open(workspaceId, node.data.entry.path);
          }}
        >
          {(props) => (
            <FileRow
              {...props}
              rootPath={rootPath}
              pending={pending}
              onManageIgnored={onManageIgnored}
              ignoreEntries={(paths) => {
                // 焦点回到未被本次操作隐藏的祖先；不触发文件激活或关闭阅读标签。
                let parent = paths[0].split("/").slice(0, -1).join("/");
                while (
                  parent &&
                  paths.some(
                    (path) => parent === path || parent.startsWith(`${path}/`),
                  )
                )
                  parent = parent.split("/").slice(0, -1).join("/");
                pendingFocus.current = parent;
                void change(paths, true).then((ok) => {
                  if (!ok) pendingFocus.current = null;
                });
              }}
              addEntries={(entries) =>
                void bridge?.add(
                  workspaceId,
                  entries.map((entry) => ({
                    path: entry.path,
                    kind: entry.kind === "directory" ? "directory" : "file",
                  })),
                )
              }
              openFile={(path, pinned) => open(workspaceId, path, pinned)}
              loadMore={loadMore}
              retry={(path) => {
                void queryClient.invalidateQueries({
                  queryKey: queryKeys.fileDirectory(
                    workspaceId,
                    path,
                    revision,
                  ),
                });
              }}
            />
          )}
        </Tree>
      )}
    </div>
  );
}

function FileRow({
  node,
  style,
  rootPath,
  openFile,
  loadMore,
  retry,
  addEntries,
  ignoreEntries,
  pending,
  onManageIgnored,
}: NodeRendererProps<TreeItem> & {
  rootPath: string;
  ignoreEntries: (paths: string[]) => void;
  pending: boolean;
  onManageIgnored: () => void;
  addEntries: (entries: FileEntry[]) => void;
  openFile: (path: string, pinned?: boolean) => void;
  loadMore: (path: string) => Promise<void>;
  retry: (path: string) => void;
}) {
  const item = node.data;
  useEffect(() => {
    if (item.more && !item.error) void loadMore(item.parent!);
  }, [item.more, item.error, item.parent, loadMore]);
  const copy = async (absolute: boolean) => {
    try {
      await navigator.clipboard.writeText(
        absolute
          ? `${rootPath.replace(/[\\/]$/, "")}/${item.entry.path}`
          : item.entry.path || ".",
      );
    } catch {
      toast.error("无法写入剪贴板");
    }
  };
  if (item.ignoredEmpty)
    return (
      <div
        style={style}
        className="flex items-center gap-2 px-3 text-xs text-muted-foreground"
      >
        <span className="truncate" title={item.entry.name}>
          {item.entry.name}
        </span>
        <button
          type="button"
          className="focus-ring shrink-0 rounded underline underline-offset-2"
          onClick={onManageIgnored}
        >
          管理忽略项
        </button>
      </div>
    );
  if (item.parent !== undefined)
    return (
      <div style={style} className="px-3 text-xs text-muted-foreground">
        <button
          type="button"
          onClick={() =>
            item.more ? void loadMore(item.parent!) : retry(item.parent!)
          }
        >
          {item.entry.name}
          {item.error ? " · 重试" : ""}
        </button>
      </div>
    );
  return (
    <ContextMenu.Root>
      <ContextMenu.Trigger asChild>
        <div
          style={style}
          data-file-path={item.entry.path}
          title={item.entry.hint ?? (item.entry.path || rootPath)}
          className={cn(
            "flex cursor-default items-center gap-1.5 rounded-md px-1 text-sm hover:bg-muted/60",
            node.isSelected && "bg-muted",
            node.isFocused && "ring-1 ring-inset ring-primary/40",
          )}
          onClick={(event) => {
            event.stopPropagation();
            node.handleClick(event);
          }}
          onDoubleClick={() => {
            if (item.entry.kind === "directory") node.toggle();
            else if (item.entry.kind === "file")
              openFile(item.entry.path, true);
          }}
        >
          {node.isInternal ? (
            <button
              type="button"
              tabIndex={-1}
              className="flex size-5 shrink-0 items-center justify-center"
              aria-label={`${node.isOpen ? "收起" : "展开"}目录 ${item.entry.name}`}
              onClick={(event) => {
                event.stopPropagation();
                if (node.isInternal) node.toggle();
              }}
            >
              {node.isInternal &&
                (node.isOpen ? (
                  <ChevronDownIcon className="size-3.5" />
                ) : (
                  <ChevronRightIcon className="size-3.5" />
                ))}
            </button>
          ) : (
            <span aria-hidden="true" className="size-5 shrink-0" />
          )}
          <WorkspaceFileIcon
            name={item.entry.name}
            folder={item.entry.kind === "directory"}
            open={node.isOpen}
          />
          <span className="truncate">{item.entry.name}</span>
          {item.entry.symlink && <LinkIcon className="size-3 shrink-0" />}
          {item.entry.hint && (
            <span className="ml-auto text-xs text-muted-foreground">
              不可展开
            </span>
          )}
        </div>
      </ContextMenu.Trigger>
      <ContextMenu.Portal>
        <ContextMenu.Content
          className={cn(MENU_CONTENT_CLASS, "min-w-40")}
          // Portal 的 React 事件仍会冒泡到树；菜单焦点和方向键由 Radix 独占。
          onFocus={(event) => event.stopPropagation()}
          onBlur={(event) => event.stopPropagation()}
          onKeyDown={(event) => event.stopPropagation()}
        >
          {item.entry.kind !== "unavailable" && (
            <ContextMenu.Item
              className={MENU_ITEM_CLASS}
              onSelect={() =>
                addEntries(
                  node.isSelected
                    ? node.tree.selectedNodes
                        .filter(
                          (n) =>
                            n.data.parent === undefined &&
                            n.data.entry.kind !== "unavailable",
                        )
                        .map((n) => n.data.entry)
                    : [item.entry],
                )
              }
            >
              添加到对话
            </ContextMenu.Item>
          )}
          {item.entry.kind === "file" && (
            <ContextMenu.Item
              className={MENU_ITEM_CLASS}
              onSelect={() => openFile(item.entry.path, true)}
            >
              固定预览
            </ContextMenu.Item>
          )}
          <ContextMenu.Item
            className={MENU_ITEM_CLASS}
            onSelect={() => void copy(false)}
          >
            复制相对路径
          </ContextMenu.Item>
          <ContextMenu.Item
            className={MENU_ITEM_CLASS}
            onSelect={() => void copy(true)}
          >
            复制绝对路径
          </ContextMenu.Item>
          <IgnoreMenuItem
            paths={(node.isSelected ? node.tree.selectedNodes : [node])
              .filter(
                (selected) =>
                  selected.data.parent === undefined &&
                  selected.data.entry.path !== "",
              )
              .map((selected) => selected.data.entry.path)}
            directory={item.entry.kind === "directory"}
            pending={pending}
            onIgnore={ignoreEntries}
          />
        </ContextMenu.Content>
      </ContextMenu.Portal>
    </ContextMenu.Root>
  );
}
