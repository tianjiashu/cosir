import { createContext, useContext, useRef, useState, type ReactNode } from "react";
import { FILE_EXITS } from "@/lib/desktop-host/file-exits";
import { CheckIcon, MoreHorizontalIcon } from "lucide-react";
import { formatByteSize } from "@/lib/format/byte-size";
import { formatRelativeTime } from "@/lib/format/relative-time";
import { copyText } from "@/lib/clipboard";
import { workspacePathText } from "@/lib/workspace-files/workspace-files-store";
import { Button } from "@/components/ui/button";
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
 *
 * 「更多」从上到下三组，各组为空就不出现：怎么看（`options`，用 `ReadingMenuItem`）、把文件交给系统、
 * 文件信息（大小与修改时间，`details` 接在后面：编码、图片尺寸这类关于文件本身的事实）。快捷键不写成说明文字，
 * 有快捷键的操作在菜单项右侧或按钮提示里经登记表展示。
 */
export function ReadingToolbar({ navigation, info, view, actions, options, details }: {
  navigation: ReadingNavigation;
  info?: FileInfo;
  view?: ReactNode;
  actions?: ReactNode;
  options?: ReactNode;
  details?: ReactNode;
}) {
  const [pathOpen, setPathOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
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
        <Popover open={moreOpen} onOpenChange={setMoreOpen}>
          <PopoverTrigger render={<Button size="icon-sm" variant="ghost" />} aria-label="更多阅读选项" title="更多阅读选项">
            <MoreHorizontalIcon />
          </PopoverTrigger>
          <PopoverContent align="end" aria-label="更多阅读选项" className="w-64 gap-0 p-1">
            <CloseMenuContext.Provider value={() => setMoreOpen(false)}>
              {options && <div className={MENU_GROUP_CLASS}>{options}</div>}
              {/* 任何文件都能交给系统：想用 Excel 打开一个 CSV，不必先去访达里找。 */}
              {FILE_EXITS.length > 0 && (
                <div className={MENU_GROUP_CLASS}>
                  {FILE_EXITS.map((exit) => (
                    <ReadingMenuItem key={exit.id}
                      onClick={() => exit.run(workspacePathText(navigation.rootPath, navigation.path, true))}>{exit.label}</ReadingMenuItem>
                  ))}
                </div>
              )}
              <FileFacts info={info}>{details}</FileFacts>
            </CloseMenuContext.Provider>
          </PopoverContent>
        </Popover>
      )}
    </div>
  );
}

/** 「更多」里的一组：组与组之间一条细线，第一组不画。 */
const MENU_GROUP_CLASS = "flex flex-col border-t py-1 first:border-t-0 first:pt-0";

/** 「更多」菜单的收起动作；只在 `ReadingToolbar` 的弹层里有值，用在别处直接报错，而不是点了不收起。 */
const CloseMenuContext = createContext<(() => void) | null>(null);

/**
 * 「更多」里的一项：左对齐整行可点。`checked` 给出时是开关（打开时右侧一个勾、`aria-pressed`；勾放右侧，
 * 文字才与其他项左对齐），`shortcut` 是右侧的按键提示，文字只经 `shortcutLabel` / `formatKeys` 生成。
 * 动作项点完收起菜单；开关与 `keepOpen`（连续点的，如缩放）留着，方便看效果或再点。
 */
export function ReadingMenuItem({ checked, shortcut, keepOpen = false, onClick, children }: {
  checked?: boolean;
  shortcut?: string;
  keepOpen?: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  const close = useContext(CloseMenuContext);
  if (!close) throw new Error("ReadingMenuItem 只能放在 ReadingToolbar 的「更多」菜单里");
  const stays = keepOpen || checked !== undefined;
  return (
    <Button size="sm" variant="ghost" className="w-full justify-start gap-2 px-2 font-normal" aria-pressed={checked}
      onClick={() => {
        if (!stays) close();
        onClick();
      }}>
      <span className="min-w-0 flex-1 truncate text-left">{children}</span>
      {shortcut && <span className="text-muted-foreground text-xs">{shortcut}</span>}
      {checked && <CheckIcon aria-hidden="true" className="size-3.5" />}
    </Button>
  );
}

/** 关于文件本身的事实：大小、修改时间，加上阅读器给的（编码、图片尺寸）。悬停看精确字节数与时间。 */
function FileFacts({ info, children }: { info: FileInfo; children?: ReactNode }) {
  const modified = new Date(info.modified_ns / 1_000_000);
  return (
    <div className="text-muted-foreground flex flex-col gap-1 border-t px-2 pt-2 pb-1 text-xs first:border-t-0 first:pt-1">
      <p title={`${info.size.toLocaleString()} 字节 · ${modified.toLocaleString()}`}>
        {formatByteSize(info.size)} · 修改于 {formatRelativeTime(modified)}
      </p>
      {children}
    </div>
  );
}
