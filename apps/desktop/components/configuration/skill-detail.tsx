import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeftIcon, FileIcon, FolderIcon, FolderOpenIcon } from "lucide-react";

import { SkillBadges, SkillWarnings } from "@/components/configuration/skill-badges";
import { skillsPath, useSkillActions } from "@/components/configuration/use-skill-actions";
import { useMarkdownLinkComponents } from "@/components/links/markdown-links";
import { StaticMarkdown } from "@/components/markdown/static-markdown";
import { ReadonlyTextViewer, type TextViewerHandle } from "@/components/text-viewer/readonly-text-viewer";
import { Button } from "@/components/ui/button";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { Switch } from "@/components/ui/switch";
import { OutlineButton } from "@/components/workspace-files/outline-button";
import { getSkill, getSkillFile, ownScope, type ExtensionScopeId, type SkillDetail as SkillDetailData } from "@/lib/api/extensions";
import { desktopHost } from "@/lib/desktop-host";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { formatByteSize } from "@/lib/format/byte-size";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { SKILL_ROOT_COPY } from "@/lib/skills/skill-directory";
import { cn } from "@/lib/utils";

const SKILL_FILE = "SKILL.md";
const LONG_DESCRIPTION_CHARS = 160;

/**
 * Skill 详情页（`…/settings/skills/:skillName`）。
 *
 * 用户打开它是为了决定「能不能放心启用」：它是什么（描述）、在哪、谁能看到（作用域、身份与位置）、会让 Agent 做什么
 * （正文与随附的脚本、参考文件）。所以页头放身份与操作，下面是「文件清单 + 阅读区」两栏，阅读区占满剩余高度、
 * 只有它自己滚动。全部内容只读：Skill 不在这里编辑或删除，要删用「在文件夹中显示」。
 */
export function SkillDetail({ workspaceId, name }: { workspaceId: ExtensionScopeId; name: string }) {
  const actions = useSkillActions(workspaceId);
  const [selectedPath, setSelectedPath] = useState(SKILL_FILE);
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
  const inherited = skill.scope !== ownScope(workspaceId);

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
              <span className="text-muted-foreground text-xs">{skill.enabled ? "已启用" : "已停用"} · 在系统设置中管理</span>
            ) : (
              <Switch checked={skill.enabled} aria-label={`${skill.enabled ? "停用" : "启用"} Skill ${skill.name}`} disabled={actions.busy} onClick={() => actions.toggle(skill)} />
            )}
            {desktopHost && (
              <Button variant="outline" size="sm" onClick={reveal}><FolderOpenIcon />在文件夹中显示</Button>
            )}
          </div>
        </div>
        <p className="text-muted-foreground flex flex-wrap items-center gap-x-1.5 text-xs">
          <span>{skill.scope === "workspace" ? "本工作区" : "系统级"} · {SKILL_ROOT_COPY[skill.kind].name}</span>
          <span aria-hidden="true">·</span>
          <code className="break-all">{skill.location}</code>
          <span aria-hidden="true">—</span>
          <span>{SKILL_ROOT_COPY[skill.kind].audience}</span>
        </p>
        {/* 软链或复制安装的同一份在列表里只列这一条；在文件夹里删除时几处会一起没，所以写出来。 */}
        {skill.also_at.length > 0 && (
          <p className="text-muted-foreground text-xs">同一份也在 {skill.also_at.map((location, index) => <span key={location}>{index > 0 && "、"}<code className="break-all">{location}</code></span>)}</p>
        )}
        <Description text={skill.description} />
        <SkillWarnings warnings={skill.warnings} />
        <Metadata metadata={skill.metadata} />
      </header>

      {/* size 包含：渲染后的 Markdown 有很大的固有高度，不包含就会把设置页内容区整个撑高、变成外层一起滚。
          包含后这块只取页头之外的剩余高度（至少 24rem），文件清单与阅读区各自滚动。 */}
      <div className="grid min-h-[24rem] flex-1 grid-rows-[auto_minmax(0,1fr)] overflow-hidden rounded-xl border [contain:size] lg:grid-cols-[14rem_minmax(0,1fr)] lg:grid-rows-[minmax(0,1fr)]">
        <FileList skill={skill} selectedPath={selectedPath} onSelect={setSelectedPath} />
        <FileReader key={selectedPath} workspaceId={workspaceId} skill={skill} path={selectedPath} />
      </div>
    </div>
  );
}

function BackLink({ workspaceId }: { workspaceId: ExtensionScopeId }) {
  return (
    <Link to={skillsPath(workspaceId)} className="text-muted-foreground hover:text-foreground focus-ring inline-flex items-center gap-1 rounded-sm text-xs">
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

/** Skill 随附的文件：按目录分组；二进制文件只列出、不提供预览。 */
function FileList({ skill, selectedPath, onSelect }: { skill: SkillDetailData; selectedPath: string; onSelect: (path: string) => void }) {
  let previousDirectory = "";
  return (
    <nav aria-label="Skill 文件" className="max-h-48 overflow-y-auto border-b p-2 lg:max-h-none lg:border-r lg:border-b-0">
      <ul className="space-y-px text-xs">
        {skill.files.map((file) => {
          const slash = file.path.lastIndexOf("/");
          const directory = slash < 0 ? "" : file.path.slice(0, slash);
          const showDirectory = directory !== "" && directory !== previousDirectory;
          previousDirectory = directory;
          const depth = directory === "" ? 0 : directory.split("/").length;
          return (
            <li key={file.path}>
              {showDirectory && (
                <div className="text-muted-foreground flex items-center gap-1.5 px-2 pt-2 pb-1" style={{ paddingLeft: `${(depth - 1) * 12 + 8}px` }}>
                  <FolderIcon className="size-3.5 shrink-0" /><span className="truncate">{directory.split("/").at(-1)}/</span>
                </div>
              )}
              <button
                type="button"
                disabled={file.binary}
                aria-current={file.path === selectedPath ? "true" : undefined}
                title={file.binary ? "二进制文件，不提供预览" : file.path}
                onClick={() => onSelect(file.path)}
                className={cn(
                  "focus-ring flex w-full items-center gap-1.5 rounded-md py-1 pr-2 text-left",
                  file.path === selectedPath ? "bg-muted text-foreground font-medium" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
                  file.binary && "cursor-default opacity-60 hover:bg-transparent",
                )}
                style={{ paddingLeft: `${depth * 12 + 8}px` }}
              >
                <FileIcon className="size-3.5 shrink-0" />
                <span className="min-w-0 flex-1 truncate">{file.path.slice(slash + 1)}</span>
                <span className="shrink-0 tabular-nums opacity-70">{file.binary ? "二进制" : formatByteSize(file.size)}</span>
              </button>
            </li>
          );
        })}
      </ul>
      {skill.files_truncated && (
        <p className="text-muted-foreground px-2 pt-2 text-xs">文件太多，只列出了一部分；完整内容请在文件夹中查看。</p>
      )}
    </nav>
  );
}

/**
 * 阅读区：SKILL.md 用详情里已有的正文，其他文件按需读取。Markdown 可切「渲染 / 源码」并用大纲跳转
 * （两种视图都按源码行号定位）；其余文本用只读代码视图。由父级按路径换 key，切文件时视图与滚动归位。
 */
function FileReader({ workspaceId, skill, path }: { workspaceId: ExtensionScopeId; skill: SkillDetailData; path: string }) {
  const isSkillFile = path === SKILL_FILE;
  const markdown = /\.(md|markdown)$/i.test(path);
  const [view, setView] = useState<"rendered" | "source">(markdown ? "rendered" : "source");
  const markdownHost = useRef<HTMLDivElement>(null);
  const textViewer = useRef<TextViewerHandle>(null);
  const linkComponents = useMarkdownLinkComponents("configuration", { webOnly: true });
  const fileQuery = useQuery({
    queryKey: queryKeys.skillFile(workspaceId, skill.name, path),
    queryFn: ({ signal }) => getSkillFile(workspaceId, skill.name, path, { signal }),
    enabled: !isSkillFile,
  });
  const text = isSkillFile ? skill.content : fileQuery.data?.content;

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
            onSelect={(heading) => {
              if (view === "rendered") markdownHost.current?.querySelector(`[data-source-line="${heading.line}"]`)?.scrollIntoView({ block: "start" });
              else textViewer.current?.revealLine(heading.line);
            }}
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
