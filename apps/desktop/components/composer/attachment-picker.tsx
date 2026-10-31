import { useState, type FC } from "react";
import { FilePlusIcon, FolderOpenIcon, PaperclipIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { DIRECTORY_CONTENT_TYPE, registerLocalFile } from "@/lib/assistant/attachments/local-attachment-registry";
import { desktopHost } from "@/lib/desktop-host";
import { pickLocalFiles } from "@/lib/desktop-host/pick-local-files";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useComposerFeedback } from "./composer-feedback";
import { insertAttachments, useComposerInsertion } from "./composer-insertion-context";
import { COMPOSER_ICON_BUTTON_CLASS_NAME } from "./composer-surface";
import { useComposerFiles } from "./use-composer-files";
import type { ComposerImageSink } from "./assistant-composer-attachments";

type AttachmentPickerProps = {
  workspaceRoot?: string;
  /** 图片交给所在输入框（新对话草稿或 assistant-ui composer）；普通文件与文件夹以行内 token 插入正文。 */
  images: ComposerImageSink;
  disabled?: boolean;
};

/**
 * 附件按钮：选文件或文件夹。
 *
 * 选文件走系统文件选择框，得到的 `File` 与粘贴、拖放一样交给 `useComposerFiles`，三个入口只有一条接纳链路。
 * 文件夹没有 `File` 可交，由宿主的目录对话框给出路径，登记后直接以 token 插入；对话框以工作区根目录为起点。
 */
export const AttachmentPicker: FC<AttachmentPickerProps> = ({ workspaceRoot, images, disabled = false }) => {
  const [opening, setOpening] = useState(false);
  const insertion = useComposerInsertion();
  const feedback = useComposerFeedback();
  const addFiles = useComposerFiles(images);

  const choose = async (directory: boolean) => {
    if (opening || disabled) return;
    setOpening(true);
    try {
      if (!directory) {
        const picked = await pickLocalFiles({ multiple: true });
        if (picked.length > 0) await addFiles(picked.map(({ file }) => file));
        return;
      }
      if (!desktopHost) throw new UserFacingError("只能在桌面应用中选择文件夹");
      const path = await desktopHost.dialog.openDirectory({ title: "选择参考文件夹", defaultPath: workspaceRoot });
      if (path === null) return;
      // 会话卸载或禁用后，迟到的选择结果只记录，不写任何草稿。
      if (!insertion.isAvailable()) {
        void frontendLog("INFO", "composer_attachment_selection_discarded", "原输入会话已关闭或禁用，忽略迟到的文件夹选择", { data: {} });
        return;
      }
      const name = path.replaceAll("\\", "/").split("/").filter(Boolean).pop() || path;
      const id = registerLocalFile({ path, name, contentType: DIRECTORY_CONTENT_TYPE });
      insertAttachments(insertion.dispatch, [{ id, name }]);
    } catch (error) {
      if (insertion.isAvailable()) feedback.show(userErrorMessage(error, "选择附件失败，请重试"));
      void frontendLog("ERROR", "attachment_picker_failed", "附件选择失败", { data: { directory }, error });
    } finally {
      setOpening(false);
    }
  };

  return (
    <Popover>
      <PopoverTrigger
        render={
          <Button
            variant="ghost"
            size="icon"
            className={COMPOSER_ICON_BUTTON_CLASS_NAME}
            aria-label={opening ? "正在选择附件" : "添加图片、文件或文件夹"}
            title="添加图片、文件或文件夹"
            disabled={opening || disabled}
          >
            <PaperclipIcon className="size-4" />
          </Button>
        }
      />
      <PopoverContent align="end" side="top" className="w-52 p-1">
        <button
          type="button"
          className="hover:bg-muted flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
          onClick={() => void choose(false)}
        >
          <FilePlusIcon className="text-muted-foreground size-4" />
          <span>选择文件或图片</span>
        </button>
        <button
          type="button"
          className="hover:bg-muted flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
          onClick={() => void choose(true)}
        >
          <FolderOpenIcon className="text-muted-foreground size-4" />
          <span>选择文件夹</span>
        </button>
      </PopoverContent>
    </Popover>
  );
};
