import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronRightIcon, FilePlusIcon, FolderOpenIcon, MessageSquareQuoteIcon, PlusIcon, PuzzleIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Command, CommandItem, CommandList } from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { ComposerImageSink } from "@/components/composer/assistant-composer-attachments";
import { useComposerFeedback } from "@/components/composer/composer-feedback";
import { insertCapsules, useComposerInsertion } from "@/components/composer/composer-insertion-context";
import { COMPOSER_ICON_BUTTON_CLASS_NAME } from "@/components/composer/composer-surface";
import { FavoritePromptManager } from "@/components/composer/favorite-prompt-manager";
import { useComposerFiles } from "@/components/composer/use-composer-files";
import { desktopHost } from "@/lib/desktop-host";
import { pickLocalFiles } from "@/lib/desktop-host/pick-local-files";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { frontendLog } from "@/lib/logging/frontend-log";
import { FavoritePromptPage } from "./favorite-prompt-page";
import { SkillPage } from "./skill-page";

type AddMenuPage = "root" | "skills" | "prompts";

type ComposerAddMenuProps = {
  /** 图片交给所在输入框（新会话草稿或 assistant-ui composer）；普通文件与文件夹以 `<file>` 胶囊插入正文。 */
  images: ComposerImageSink;
  /** 文件夹对话框的起点。 */
  workspaceRoot?: string;
  /** Skill 页列出这个工作区看得到的 Skill；`null` 只列系统级。 */
  workspaceId: number | null;
  disabled?: boolean;
};

/** 目录胶囊的路径按语法约定以 `/` 结尾；宿主返回的本机路径可能带反斜杠或已有尾斜杠。 */
function asDirectoryCapsulePath(path: string): string {
  return `${path.replace(/[\\/]+$/, "")}/`;
}

/**
 * 输入框的「+」添加菜单：往正文里放内容的唯一入口，新会话页与会话页共用。
 *
 * 第一层四行：文件或图片、文件夹直接打开选择框；Skill、常用提示词进入二级页（可搜索）。插入一律经
 * `ComposerInsertionContext` 落在用户最后的光标处，菜单不知道自己在哪个输入框里。
 * 选文件走系统文件选择框，得到的 `File` 与粘贴、拖放一样交给 `useComposerFiles`，只有一条接纳链路；
 * 文件夹没有 `File` 可交，由宿主的目录对话框给出路径，直接以目录胶囊插入，对话框以工作区根目录为起点。
 * 打开选择框前先收起菜单；选择框迟到返回时输入框可能已关闭或禁用，此时只记录、不写草稿。
 * 提示词库的管理对话框由菜单持有：从提示词页点「管理」后菜单收起，对话框仍在。
 */
export function ComposerAddMenu({ images, workspaceRoot, workspaceId, disabled = false }: ComposerAddMenuProps) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState<AddMenuPage>("root");
  const [picking, setPicking] = useState(false);
  const [managerOpen, setManagerOpen] = useState(false);
  const insertion = useComposerInsertion();
  const feedback = useComposerFeedback();
  const addFiles = useComposerFiles(images);
  // 第一层没有输入框：键盘焦点放在 cmdk 根节点上，方向键与回车才生效；从二级页返回时同样要放回来。
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (open && page === "root") rootRef.current?.focus();
  }, [open, page]);

  const changeOpen = (next: boolean) => {
    setOpen(next);
    if (!next) setPage("root");
  };

  const pick = async (directory: boolean) => {
    if (picking) return;
    changeOpen(false);
    setPicking(true);
    try {
      if (!directory) {
        const picked = await pickLocalFiles({ multiple: true });
        if (picked.length > 0) await addFiles(picked.map(({ file }) => file));
        return;
      }
      if (!desktopHost) throw new UserFacingError("只能在桌面应用中选择文件夹");
      const path = await desktopHost.dialog.openDirectory({ title: "选择参考文件夹", defaultPath: workspaceRoot });
      if (path === null) return;
      if (!insertion.isAvailable()) {
        void frontendLog("INFO", "composer_attachment_selection_discarded", "原输入会话已关闭或禁用，忽略迟到的文件夹选择", { data: {} });
        return;
      }
      insertCapsules(insertion.dispatch, [{ kind: "file", path: asDirectoryCapsulePath(path) }]);
    } catch (error) {
      if (insertion.isAvailable()) feedback.show(userErrorMessage(error, "选择附件失败，请重试"));
      void frontendLog("ERROR", "attachment_picker_failed", "附件选择失败", { data: { directory }, error });
    } finally {
      setPicking(false);
    }
  };

  return (
    <>
      <Popover open={open} onOpenChange={changeOpen}>
        <PopoverTrigger
          render={
            <Button
              variant="ghost"
              size="icon"
              className={COMPOSER_ICON_BUTTON_CLASS_NAME}
              aria-label={picking ? "正在选择附件" : "添加"}
              title="添加文件、Skill 或提示词"
              disabled={disabled || picking}
            >
              <PlusIcon />
            </Button>
          }
        />
        <PopoverContent align="start" side="top" sideOffset={6} initialFocus={rootRef} className="w-80 gap-0 rounded-xl p-0 shadow-lg">
          {page === "root" && (
            <Command ref={rootRef} aria-label="添加" className="outline-none">
              <CommandList className="p-1">
                <AddMenuRow icon={<FilePlusIcon />} title="文件或图片" description="图片随消息发送，其他文件以引用插入" onSelect={() => void pick(false)} />
                <AddMenuRow icon={<FolderOpenIcon />} title="文件夹" description="以引用插入，Agent 按需读取" onSelect={() => void pick(true)} />
                <AddMenuRow icon={<PuzzleIcon />} title="Skill" description="让 Agent 按 Skill 的步骤做" drill onSelect={() => setPage("skills")} />
                <AddMenuRow icon={<MessageSquareQuoteIcon />} title="常用提示词" description="插入保存过的提示词" drill onSelect={() => setPage("prompts")} />
              </CommandList>
            </Command>
          )}
          {page === "skills" && <SkillPage workspaceId={workspaceId} onBack={() => setPage("root")} onPicked={() => changeOpen(false)} />}
          {page === "prompts" && (
            <FavoritePromptPage
              onBack={() => setPage("root")}
              onPicked={() => changeOpen(false)}
              onManage={() => {
                changeOpen(false);
                setManagerOpen(true);
              }}
            />
          )}
        </PopoverContent>
      </Popover>
      <FavoritePromptManager open={managerOpen} onOpenChange={setManagerOpen} />
    </>
  );
}

/** 第一层的一行：图标、名称与一句说明；`drill` 表示进入二级页，行尾带箭头。 */
function AddMenuRow({ icon, title, description, drill = false, onSelect }: {
  icon: ReactNode;
  title: string;
  description: string;
  drill?: boolean;
  onSelect: () => void;
}) {
  return (
    <CommandItem value={title} onSelect={onSelect} className="gap-2.5 rounded-lg px-2.5 py-2 [&>svg:last-child]:hidden [&_svg]:size-4">
      <span className="text-muted-foreground shrink-0">{icon}</span>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="text-sm">{title}</span>
        <span className="text-muted-foreground truncate text-xs">{description}</span>
      </span>
      {drill && <ChevronRightIcon className="text-muted-foreground shrink-0" aria-hidden="true" />}
    </CommandItem>
  );
}
