import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeftIcon, FolderOpenIcon } from "lucide-react";

import { SkillBadges, SkillWarnings } from "@/components/configuration/skills/skill-badges";
import { SkillFileTree, useSkillFileTreeState } from "@/components/configuration/skills/skill-file-tree";
import { useSkillLinkOpener } from "@/components/configuration/skills/skill-link-opener";
import { useSkillActions } from "@/components/configuration/skills/use-skill-actions";
import { configurationPath } from "@/components/configuration/configuration-tabs";
import { useMarkdownLinkComponents } from "@/components/links/markdown-links";
import { StaticMarkdown } from "@/components/markdown/static-markdown";
import { ReadonlyTextViewer, type TextViewerHandle } from "@/components/text-viewer/readonly-text-viewer";
import { Button } from "@/components/ui/button";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { Switch } from "@/components/ui/switch";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { OutlineButton, revealSourceLine } from "@/components/markdown/outline-button";
import { scopeOf } from "@/lib/api/configuration-scope";
import { getSkill, getSkillFile, type SkillDetail } from "@/lib/api/extensions";
import { desktopHost } from "@/lib/desktop-host";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { SKILL_ROOT_COPY, scopedRootLabel } from "@/lib/skills/skill-directory";
import { SKILL_ENTRY_FILE } from "@/lib/skills/skill-file-tree";
import { cn } from "@/lib/utils";
import { parentPath } from "@/lib/workspace-files/workspace-files-store";

const LONG_DESCRIPTION_CHARS = 160;

/**
 * Skill 详情页（`…/configuration/skills/:skillName`）。
 *
 * 用户打开它是为了决定「能不能放心启用」：它是什么（描述）、在哪、谁能看到（作用域、身份与位置）、会让 Agent 做什么
 * （正文与随附的脚本、参考文件）。所以页头放身份与操作，下面是「文件清单 + 阅读区」两栏，阅读区占满剩余高度、
 * 只有它自己滚动。全部内容只读：Skill 不在这里编辑或删除，要删用「在文件夹中显示」。
 */
export function SkillDetailPage({ workspaceId, name }: { workspaceId: number | null; name: string }) {
  const actions = useSkillActions(workspaceId);
  const tree = useSkillFileTreeState();
  const detailQuery = useQuery({
    queryKey: queryKeys.skill(workspaceId, name),
    queryFn: ({ signal }) => getSkill(workspaceId, name, { signal }),
  });

  if (detailQuery.isPending) return <LoadingState label="正在读取 Skill…" />;
  if (detailQuery.isError) {
    return (
      <div className="space-y-4">
        <BackLink workspaceId={workspaceId} />
        <ErrorState message={userErrorMessage(detailQuery.error, "读取 Skill 详情失败")} onRetry={() => void detailQuery.refetch()} retryLabel="重新读取" />
      </div>
    );
  }

  const skill = detailQuery.data;
  // 工作区页里继承来的系统级 Skill 只读。
  const inherited = skill.scope !== scopeOf(workspaceId);

  const reveal = () => {
    desktopHost!.files.reveal(skill.directory).catch((error: unknown) => {
      void frontendLog("WARNING", "skill_reveal_failed", "在文件夹中显示 Skill 失败", { data: { skill: skill.name }, error });
      notify.error("无法在文件夹中显示", { error });
    });
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      <header className="space-y-2">
        <BackLink workspaceId={workspaceId} />
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold break-all">{skill.name}</h2>
            <SkillBadges skill={skill} />
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {inherited ? (
              <span className="text-muted-foreground text-xs">{skill.enabled ? "已启用" : "已停用"} · 在系统配置中管理</span>
            ) : (
              <Switch checked={skill.enabled} aria-label={`${skill.enabled ? "停用" : "启用"} Skill ${skill.name}`} disabled={actions.busy} onClick={() => actions.setEnabled([skill.name], !skill.enabled)} />
            )}
            {desktopHost && (
              <Button variant="outline" size="sm" onClick={reveal}><FolderOpenIcon />在文件夹中显示</Button>
            )}
          </div>
        </div>
        <p className="text-muted-foreground flex flex-wrap items-center gap-x-1.5 text-xs">
          <span>{scopedRootLabel(skill.scope, skill.kind)}</span>
          <span aria-hidden="true">·</span>
          <code className="break-all">{skill.location}</code>
          <span aria-hidden="true">—</span>
          <span>{SKILL_ROOT_COPY[skill.kind].audience}</span>
          {/* 软链或复制安装的同一份在列表里只列这一条；在文件夹里删除时几处会一起没，所以留个入口，悬停列出位置。 */}
          {skill.also_at.length > 0 && (
            <Tooltip>
              <TooltipTrigger className="hover:text-foreground focus-ring rounded-sm underline decoration-dotted underline-offset-2">· 同一份也在 {skill.also_at.length} 处</TooltipTrigger>
              <TooltipContent className="flex-col items-start font-mono">{skill.also_at.map((location) => <span key={location} className="break-all">{location}</span>)}</TooltipContent>
            </Tooltip>
          )}
        </p>
        <Description text={skill.description} />
        <SkillWarnings warnings={skill.warnings} />
        <Metadata metadata={skill.metadata} />
      </header>

      {/* size 包含：渲染后的 Markdown 有很大的固有高度，不包含就会把配置页内容区整个撑高、变成外层一起滚。
          包含后这块只取页头之外的剩余高度（至少 24rem），文件清单与阅读区各自滚动。 */}
      <div className="grid min-h-[24rem] flex-1 grid-rows-[auto_minmax(0,1fr)] overflow-hidden rounded-xl border [contain:size] lg:grid-cols-[14rem_minmax(0,1fr)] lg:grid-rows-[minmax(0,1fr)]">
        <SkillFileTree files={skill.files} truncated={skill.files_truncated} selected={tree.selected} expanded={tree.expanded} onSelect={tree.select} onToggle={tree.toggle} />
        <SkillFileReader key={tree.selected} workspaceId={workspaceId} skill={skill} path={tree.selected} onOpenFile={tree.select} />
      </div>
    </div>
  );
}

function BackLink({ workspaceId }: { workspaceId: number | null }) {
  return (
    <Link to={configurationPath(workspaceId, "skills")} className="text-muted-foreground hover:text-foreground focus-ring inline-flex items-center gap-1 rounded-sm text-xs">
      <ArrowLeftIcon className="size-3.5" />Skills
    </Link>
  );
}

/** 描述可能很长（触发词清单）：默认收成三行，读正文前不必先读完它。 */
function Description({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const long = text.length > LONG_DESCRIPTION_CHARS;
  return (
    <div className="text-sm">
      <p className={cn("text-foreground/90 whitespace-pre-line", long && !expanded && "line-clamp-3")}>{text}</p>
      {long && (
        <button type="button" className="text-muted-foreground hover:text-foreground focus-ring mt-0.5 rounded-sm text-xs" onClick={() => setExpanded((value) => !value)}>
          {expanded ? "收起" : "展开全部描述"}
        </button>
      )}
    </div>
  );
}

/** frontmatter 里标准两项之外的字段原样列出（license、allowed-tools 及各产品扩展），cosir 不解释它们。 */
function Metadata({ metadata }: { metadata: Record<string, string> }) {
  const entries = Object.entries(metadata);
  if (entries.length === 0) return null;
  return (
    <details className="text-xs">
      <summary className="text-muted-foreground hover:text-foreground cursor-pointer select-none">其他元数据（{entries.length}）</summary>
      <dl className="mt-2 grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-1">
        {entries.map(([key, value]) => (
          <div key={key} className="contents">
            <dt className="text-muted-foreground font-mono">{key}</dt>
            <dd className="font-mono break-all whitespace-pre-wrap">{value}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

/**
 * 阅读区：SKILL.md 用详情里已有的正文，其他文件按需读取。Markdown 可切「渲染 / 源码」并用大纲跳转
 * （两种视图都按源码行号定位）；其余文本用只读代码视图。由父级按路径换 key，切文件时视图与滚动归位。
 * 渲染视图里指向 Skill 自己文件的链接与行内代码路径，点了经 `onOpenFile` 切到那个文件。
 */
function SkillFileReader({ workspaceId, skill, path, onOpenFile }: { workspaceId: number | null; skill: SkillDetail; path: string; onOpenFile: (path: string) => void }) {
  const isEntryFile = path === SKILL_ENTRY_FILE;
  const markdown = /\.(md|markdown)$/i.test(path);
  const [view, setView] = useState<"rendered" | "source">(markdown ? "rendered" : "source");
  const markdownHost = useRef<HTMLDivElement>(null);
  const textViewer = useRef<TextViewerHandle>(null);
  const linkComponents = useMarkdownLinkComponents(useSkillLinkOpener(skill, onOpenFile), {
    documentDir: parentPath(path),
  });
  const fileQuery = useQuery({
    queryKey: queryKeys.skillFile(workspaceId, skill.name, path),
    queryFn: ({ signal }) => getSkillFile(workspaceId, skill.name, path, { signal }),
    enabled: !isEntryFile,
  });
  const text = isEntryFile ? skill.content : fileQuery.data?.content;

  return (
    <section aria-label={`阅读 ${path}`} className="flex min-h-0 min-w-0 flex-col">
      <div className="flex h-11 shrink-0 items-center gap-2 border-b px-3">
        <span className="min-w-0 flex-1 truncate font-mono text-xs">{path}</span>
        {markdown && (
          <SegmentedControl
            value={view}
            options={[{ value: "rendered", label: "渲染" }, { value: "source", label: "源码" }]}
            onValueChange={setView}
            aria-label="阅读方式"
          />
        )}
        {markdown && text !== undefined && (
          <OutlineButton
            text={text}
            partial={false}
            onSelect={({ line }) => revealSourceLine(line, view === "rendered", markdownHost.current, textViewer.current)}
          />
        )}
      </div>
      {text === undefined ? (
        fileQuery.isError
          ? <ErrorState className="m-4" message={userErrorMessage(fileQuery.error, "读取文件失败")} onRetry={() => void fileQuery.refetch()} retryLabel="重新读取" />
          : <LoadingState label="正在读取文件…" />
      ) : view === "rendered" ? (
        <div ref={markdownHost} className="aui-md min-h-0 flex-1 overflow-y-auto px-6 py-5 leading-6">
          <StaticMarkdown components={linkComponents} className="!space-y-3">{text}</StaticMarkdown>
        </div>
      ) : (
        <div className="min-h-0 flex-1 text-xs">
          <ReadonlyTextViewer ref={textViewer} text={text} name={path} ariaLabel={`只读文件 ${path}`} wrap scrollTop={0} onScroll={() => {}} />
        </div>
      )}
    </section>
  );
}
