import { createContext, useContext, type ReactNode } from "react";
import { BotIcon, ChevronDownIcon, ChevronRightIcon, FolderIcon, GitForkIcon, PinIcon } from "lucide-react";
import type { NodeApi, NodeRendererProps, RowRendererProps } from "react-arborist";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatRelativeTime } from "@/lib/format/relative-time";
import { cn } from "@/lib/utils";
import { AWAITING_USER_LABEL, SessionStatusMarker } from "@/components/session-status/session-status-indicator";
import { SESSION_PAGE_SIZE, type SessionSidebarNode, type WorkspaceSidebarNode, type WorkspaceTreeSource } from "./workspace-tree-model";
import { useSessionAwaitingUserCalls } from "@/lib/session-status/session-status-store";

/**
 * 当前项由路由与工作区选择决定；树的键盘焦点不构成第二份导航状态。
 *
 * 工作区与会话行整行是悬停卡片的触发区（键盘聚焦同样弹出），卡片内容见 `RowHoverCard`；
 * 延迟与相邻行之间的即时切换来自应用根部唯一的 TooltipProvider（`App`）。
 */
export function WorkspaceTreeRow({ node, innerRef, attrs, children }: RowRendererProps<WorkspaceSidebarNode>) {
  const item = node.data;
  const current = node.id === node.tree.props.selection;
  const row = (
    <div
      {...attrs}
      ref={innerRef}
      style={{ ...attrs.style, minWidth: 0 }}
      className="workspace-tree-row"
      aria-label={sidebarNodeLabel(item)}
      aria-selected={current}
      aria-current={current ? (item.kind === "session" ? "page" : "true") : undefined}
      data-current={current || undefined}
      data-kind={item.kind}
      onFocus={(event) => event.stopPropagation()}
      onClick={node.handleClick}
      onKeyDown={(event) => {
        // 行内按钮保留原生键盘行为，不让树拦截 Tab、空格或方向键。
        if (event.target !== event.currentTarget || event.key === "Tab") {
          event.stopPropagation();
        } else if (event.key === "Enter") {
          event.preventDefault();
          event.stopPropagation();
          node.activate();
        }
      }}
    >
      {children}
    </div>
  );
  if (item.kind !== "workspace" && item.kind !== "session") return row;
  return (
    <Tooltip>
      <TooltipTrigger render={row} />
      <TooltipContent side="right" align="start" sideOffset={8} className="flex-col items-start gap-1 break-all">
        <RowHoverCard item={item} />
      </TooltipContent>
    </Tooltip>
  );
}

const CREATED_AT_FORMAT = new Intl.DateTimeFormat("zh-CN", { dateStyle: "long", timeStyle: "short" });

/** 悬停卡片：工作区给出界面上别处看不到的完整路径（同名工作区靠它区分），会话给出完整标题。 */
function RowHoverCard({ item }: { item: Extract<WorkspaceSidebarNode, { kind: "workspace" | "session" }> }) {
  if (item.kind === "workspace") {
    return (
      <>
        <span className="text-sm">{item.workspace.name}</span>
        <span className="opacity-75">{item.workspace.root_path}</span>
        <span className="opacity-75">创建于 {CREATED_AT_FORMAT.format(new Date(item.workspace.created_at))}</span>
      </>
    );
  }
  return (
    <>
      <span className="text-sm">{item.session.full_title}</span>
      {item.workspaceName && <span className="opacity-75">工作区：{item.workspaceName}</span>}
    </>
  );
}

function sidebarNodeLabel(item: WorkspaceSidebarNode): string {
  switch (item.kind) {
    case "session": return item.session.full_title;
    case "workspace": return item.workspace.name;
    case "pinned": return PINNED_GROUP_LABEL;
    case "placeholder": return item.label;
    case "more": return moreLabel(item.hiddenCount);
  }
}

function moreLabel(hiddenCount: number): string {
  if (hiddenCount === 0) return "收起";
  return hiddenCount <= SESSION_PAGE_SIZE ? `展开其余 ${hiddenCount} 个` : `显示更多（还有 ${hiddenCount} 个）`;
}

const PINNED_GROUP_LABEL = "置顶";

/** 行尾操作全部由侧栏装配；树只负责摆放与键盘交互，不知道具体有哪些操作。 */
type RowActions = {
  renderWorkspaceActions: (workspace: WorkspaceTreeSource, tabIndex: number) => ReactNode;
  renderSessionActions: (item: SessionSidebarNode, tabIndex: number) => ReactNode;
};

// Tree 将 renderer 当组件挂载；通过上下文传操作，避免回调变化时重挂载并丢失焦点或菜单。
export const WorkspaceTreeActionsContext = createContext<RowActions | null>(null);

export function WorkspaceTreeNode({ node, style }: NodeRendererProps<WorkspaceSidebarNode>) {
  // 此 renderer 始终由 WorkspaceTree 的 Provider 装配。
  const { renderWorkspaceActions, renderSessionActions } = useContext(WorkspaceTreeActionsContext)!;
  const item = node.data;
  // 只让焦点行的操作进入 Tab 序列，方向键移动仍交给 react-arborist。
  const tabIndex = node.isFocused ? 0 : -1;
  // 占位与「显示更多」行和顶层会话一样不随层级缩进，文字与会话标题左对齐（标记列 + 间距）。
  const flushStyle = { ...style, paddingLeft: SESSION_MARKER_WIDTH + SESSION_MARKER_GAP };
  if (item.kind === "placeholder") {
    return <div style={flushStyle} className={cn("workspace-tree-node", item.isError ? "text-destructive" : "text-muted-foreground")}><span className="truncate text-xs">{item.label}</span></div>;
  }
  if (item.kind === "more") {
    // 激活（点击、Enter）交给树的 onActivate，不在这里另挂按钮。
    return <div style={flushStyle} className="workspace-tree-node text-muted-foreground"><span className="workspace-tree-title truncate text-xs">{moreLabel(item.hiddenCount)}</span></div>;
  }
  if (item.kind === "pinned") {
    return (
      <GroupRow node={node} style={style} tabIndex={tabIndex} icon={<PinIcon className="size-4" />} label={PINNED_GROUP_LABEL} toggleLabel={`${node.isOpen ? "收起" : "展开"}置顶会话`} />
    );
  }
  if (item.kind === "workspace") {
    const workspace = item.workspace;
    // 工作区行只展开 / 收起：选中哪个工作区由「+」、新会话页的选择器或打开会话显式决定，点击行不改选择。
    return (
      <GroupRow
        node={node}
        style={style}
        tabIndex={tabIndex}
        icon={<FolderIcon className="size-4" />}
        label={workspace.name}
        toggleLabel={`工作区 ${workspace.name}，${node.isOpen ? "点击收起" : "点击展开"}`}
      >
        {workspace.sessionLoadError && <span className="shrink-0 text-xs text-destructive" title={workspace.sessionLoadError}>加载失败</span>}
        <div className="workspace-tree-actions" onClick={(event) => event.stopPropagation()}>{renderWorkspaceActions(workspace, tabIndex)}</div>
      </GroupRow>
    );
  }
  return <WorkspaceSessionRow node={node} style={style} item={item} actions={renderSessionActions(item, tabIndex)} />;
}

/**
 * 分组行（工作区、置顶）：图标格平时显示分组图标，悬停或聚焦时换成展开箭头（CSS 切换），
 * 宽度与会话行的状态标记列相同，所以分组名与会话标题左对齐、图标与状态点同列。
 */
function GroupRow({ node, style, tabIndex, icon, label, toggleLabel, children }: Pick<NodeRendererProps<WorkspaceSidebarNode>, "node" | "style"> & {
  tabIndex: number;
  icon: ReactNode;
  label: string;
  toggleLabel: string;
  children?: ReactNode;
}) {
  return (
    <div style={style} className="workspace-tree-node">
      <button
        type="button"
        tabIndex={tabIndex}
        className="focus-ring flex min-w-0 flex-1 items-center gap-2 self-stretch rounded-md text-left text-sm"
        aria-expanded={node.isOpen}
        aria-label={toggleLabel}
        onClick={(event) => { event.stopPropagation(); node.toggle(); }}
      >
        <span className="workspace-tree-group-icon text-muted-foreground" style={{ width: SESSION_MARKER_WIDTH }} aria-hidden="true">
          <span className="workspace-tree-group-glyph">{icon}</span>
          <ChevronRightIcon className="workspace-tree-group-chevron size-3.5" data-open={node.isOpen || undefined} />
        </span>
        <span className="workspace-tree-title truncate">{label}</span>
      </button>
      {children}
    </div>
  );
}

/** workspace 在第 0 层、顶层会话在第 1 层；更深的都是挂在源会话下的 Fork。 */
const ROOT_SESSION_LEVEL = 1;
/**
 * 顶层会话不随 workspace 层缩进，状态标记列对齐分组行的图标格；
 * Fork 每深一层只缩 12px，避免多层后标题被挤窄。
 */
const ROOT_SESSION_INDENT = 0;
const FORK_INDENT = 12;
/** 状态标记列宽：只容纳状态点或 Fork 图标；引导线位置据此推算，宽度只在这里定义。 */
const SESSION_MARKER_WIDTH = 20;
/** 标记与标题的间距，与 `.workspace-tree-session` 的 `gap` 一致。 */
const SESSION_MARKER_GAP = 8;

type BranchGuide = "pass" | "none" | "tee" | "end";

/**
 * 计算 Fork 行左侧的树形引导线：每层祖先占一列，最后一列是连到父会话的 ├ / └。
 *
 * 祖先那一列是否继续往下画，取决于路径上低一层的祖先后面还有没有兄弟；
 * 只读 react-arborist 的可见节点关系，模型里不另存深度或「是否末子」。
 */
function branchGuides(node: NodeApi<WorkspaceSidebarNode>): BranchGuide[] {
  const guides: BranchGuide[] = [node.nextSibling ? "tee" : "end"];
  for (let ancestor = node.parent; ancestor && ancestor.level > ROOT_SESSION_LEVEL; ancestor = ancestor.parent) {
    guides.unshift(ancestor.nextSibling ? "pass" : "none");
  }
  return guides;
}

function WorkspaceSessionRow({ node, style, item, actions }: Pick<NodeRendererProps<WorkspaceSidebarNode>, "node" | "style"> & { item: SessionSidebarNode; actions: ReactNode }) {
  const session = item.session;
  const awaitingUser = useSessionAwaitingUserCalls(session.session_id) > 0;
  const nested = node.level > ROOT_SESSION_LEVEL;
  // 嵌套行的 Fork 身份由层级表达；源会话已不在列表中、被提为顶层的 Fork 仍保留图标。Agent 用
  // `fork_session` 分出的对话无论在哪一层都标 Bot：它不是用户自己开的分支。
  const agentFork = session.session_type === "agent_fork";
  const showForkIcon = session.session_type === "user_fork" && !nested;
  // 有事等你操作时暂停图标独占标记位（「等你操作优先」），身份图标让位，行尾同时写明。
  const showMarkerIcon = (showForkIcon || agentFork) && !awaitingUser;
  // 覆盖 react-arborist 按层级等距算出的 paddingLeft，只收紧 Fork 层。
  const paddingLeft = ROOT_SESSION_INDENT + (node.level - ROOT_SESSION_LEVEL) * FORK_INDENT;
  return (
    <div style={{ ...style, paddingLeft }} className="workspace-tree-node workspace-tree-session">
      {nested && branchGuides(node).map((guide, index) => (
        // 第 i 列落在对应祖先标题起点左侧 4px；横线 8px 恰好到下一层标记右缘，状态点压在线尾。
        <span key={index} className="workspace-tree-guide" data-guide={guide} style={{ left: ROOT_SESSION_INDENT + index * FORK_INDENT + SESSION_MARKER_WIDTH + SESSION_MARKER_GAP - 4 }} aria-hidden="true" />
      ))}
      <span className="workspace-tree-session-marker" style={{ flexBasis: SESSION_MARKER_WIDTH }} data-nested={nested || undefined}>
        {showMarkerIcon && showForkIcon && <GitForkIcon className="size-3.5 text-muted-foreground" aria-label="Fork 会话" />}
        {showMarkerIcon && agentFork && <BotIcon className="size-3.5 text-muted-foreground" aria-label="Agent 分出的对话" />}
        <SessionStatusMarker sessionId={session.session_id} dotClassName={showMarkerIcon ? "absolute -top-0.5 right-0 size-1.5" : undefined} />
      </span>
      <span className="flex min-w-0 flex-1 items-center gap-1">
        <span className="workspace-tree-title min-w-0 truncate">{session.full_title}</span>
        {node.isInternal && (
          // 不进 Tab 序列：键盘用户以左右方向键展开 / 收起，与 workspace 行一致地交给 react-arborist。
          <button
            type="button"
            tabIndex={-1}
            className="flex size-5 shrink-0 items-center justify-center rounded text-muted-foreground hover:text-foreground"
            aria-expanded={node.isOpen}
            aria-label={`${node.isOpen ? "收起" : "展开"} ${session.full_title} 的 Fork`}
            onClick={(event) => { event.stopPropagation(); node.toggle(); }}
          >
            {node.isOpen ? <ChevronDownIcon className="size-3.5" /> : <ChevronRightIcon className="size-3.5" />}
          </button>
        )}
      </span>
      {/* 元信息与操作叠在同一尾栏格内，宽度取较宽者；操作显示时隐藏时间，标题不跳动。 */}
      <div className="workspace-tree-session-tail">
        {awaitingUser
          ? <span className="workspace-tree-time whitespace-nowrap text-2xs text-info">{AWAITING_USER_LABEL}</span>
          : <time className="workspace-tree-time whitespace-nowrap text-2xs tabular-nums text-muted-foreground" dateTime={session.updated_at}>{formatRelativeTime(session.updated_at)}</time>}
        <div className="workspace-tree-actions" onClick={(event) => event.stopPropagation()}>{actions}</div>
      </div>
    </div>
  );
}
