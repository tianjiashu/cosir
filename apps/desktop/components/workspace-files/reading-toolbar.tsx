import { useRef, useState, type ReactNode } from "react";
import { CodeXmlIcon, EyeIcon, MoreHorizontalIcon } from "lucide-react";
import { copyText } from "@/lib/clipboard";
import { workspacePathText, type ReadingView } from "@/lib/workspace-files/store";
import { Button } from "@/components/ui/button";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { Popover, PopoverContent, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import type { FileInfo } from "@/lib/api/workspace-files";

/**
 * 文件面板正文的第一行：目录栏的文件名搜索与阅读栏的工具栏共用同一行高，左右两栏因此对齐。
 * 与面板头部一样不画分割线，靠留白分隔。
 */
export const READING_ROW_CLASS = "flex h-11 shrink-0 items-center gap-1 px-2";

export type ReadingNavigation = {
  path: string;
  rootPath: string;
  revealPath: () => void;
};

/**
 * 阅读器共用的唯一一条操作栏：左侧路径（点开可复制或在目录中定位），右侧依次是视图切换、预览器自身的操作
 * 与「更多」弹层。各预览器（文本、图片、PDF）自己渲染它并只往槽位里放自身控件，不再另起一行工具栏；
 * 目录开关在面板头部，不在这里。
 */
export function ReadingToolbar({ navigation, info, view, actions, options }: {
  navigation: ReadingNavigation;
  info?: FileInfo;
  view?: ReactNode;
  actions?: ReactNode;
  options?: ReactNode;
}) {
  const [pathOpen, setPathOpen] = useState(false);
  const pathTrigger = useRef<HTMLButtonElement>(null);
  const copy = (absolute: boolean) => copyText(workspacePathText(navigation.rootPath, navigation.path, absolute), {
    success: "路径已复制",
    logEvent: "workspace_path_copy_failed",
    logData: { absolute, source: "reading_toolbar" },
  });
  return (
    <div className={READING_ROW_CLASS} aria-label="文件阅读工具栏">
      <Popover open={pathOpen} onOpenChange={setPathOpen}>
        <PopoverTrigger render={<button ref={pathTrigger} type="button" className="min-w-0 flex-1 truncate rounded px-1 py-1.5 text-left text-sm text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring" />}
          title={navigation.path} aria-label={`文件路径 ${navigation.path}`}>
          {navigation.path}
        </PopoverTrigger>
        <PopoverContent align="start" finalFocus={() => pathTrigger.current?.getClientRects().length ? pathTrigger.current : false}>
          <PopoverTitle className="break-all">{navigation.path}</PopoverTitle>
          <Button size="sm" variant="ghost" onClick={() => void copy(false)}>复制相对路径</Button>
          <Button size="sm" variant="ghost" onClick={() => void copy(true)}>复制绝对路径</Button>
          <Button size="sm" variant="ghost" onClick={() => { setPathOpen(false); navigation.revealPath(); }}>在目录中定位当前文件</Button>
        </PopoverContent>
      </Popover>
      <div className="flex shrink-0 items-center gap-0.5 text-xs">{view}{actions}</div>
      {info && (
        <Popover>
          <PopoverTrigger render={<Button size="icon-sm" variant="ghost" />} aria-label="更多阅读选项" title="更多阅读选项">
            <MoreHorizontalIcon />
          </PopoverTrigger>
          <PopoverContent align="end">
            <PopoverTitle>阅读选项</PopoverTitle>
            {options}
            <div className="border-t pt-2 text-xs text-muted-foreground">
              <p>文件信息 · {info.size.toLocaleString()} 字节</p>
              <p>{new Date(info.modified_ns / 1_000_000).toLocaleString()}</p>
            </div>
          </PopoverContent>
        </Popover>
      )}
    </div>
  );
}

/** 渲染视图与源码之间的一键切换：图标表示点下去会去哪一边。 */
export function ViewToggle({ view, onChange }: {
  view: ReadingView;
  onChange: (view: ReadingView) => void;
}) {
  const label = view === "rendered" ? "查看源码" : "查看预览";
  return (
    <TooltipIconButton tooltip={label} aria-label={label} className="size-8 shrink-0"
      onClick={() => onChange(view === "rendered" ? "source" : "rendered")}>
      {view === "rendered" ? <CodeXmlIcon /> : <EyeIcon />}
    </TooltipIconButton>
  );
}
