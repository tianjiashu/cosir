"use client";

import { useState, type FC } from "react";
import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import { FilePlusIcon, FolderOpenIcon, PaperclipIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import {
  contentTypeFor,
  DIRECTORY_CONTENT_TYPE,
  fileName,
  isImagePath,
  isUnsupportedImagePath,
} from "@/components/composer/attachment-policy";
import { useComposerInsertion } from "./composer-insertion-context";
import { registerLocalAttachment } from "@/lib/assistant/attachments/local-attachment-registry";

export type PickedComposerAttachment =
  | { id: string; kind: "image"; path: string; name: string; file: File }
  | { id: string; kind: "file"; path: string; name: string; file: File };

type AttachmentPickerProps = {
  workspaceRoot?: string;
  onPicked: (attachments: PickedComposerAttachment[]) => void | Promise<void>;
  onError?: (message: string) => void;
  disabled?: boolean;
};

async function readSelectedFile(path: string, name: string): Promise<File> {
  const bytes = await invoke<number[]>("read_selected_attachment_file", {
    path,
  });
  return new File([new Uint8Array(bytes)], name, { type: contentTypeFor(path) });
}

function createEmptyAttachmentFile(name: string, contentType: string): File {
  return new File([], name, { type: contentType });
}

export const AttachmentPicker: FC<AttachmentPickerProps> = ({
  workspaceRoot,
  onPicked,
  onError,
  disabled = false,
}) => {
  const [opening, setOpening] = useState(false);
  const insertion = useComposerInsertion();

  // 文件与目录共用一次接纳边界；会话卸载/禁用后，迟到结果只记录，不写任何草稿。
  const choose = async (directory: boolean) => {
    if (opening || disabled) return;
    setOpening(true);
    try {
      const selected = await open({
        multiple: !directory,
        directory,
        defaultPath: workspaceRoot,
        title: directory ? "选择参考文件夹" : "选择图片或参考附件",
      });
      const paths = selected === null ? [] : Array.isArray(selected) ? selected : [selected];
      const picked: PickedComposerAttachment[] = [];
      let unsupportedImageCount = 0;
      for (const selectedPath of paths) {
        const path = await invoke<string>("resolve_selected_attachment_path", { path: selectedPath });
        const name = fileName(path);
        const contentType = directory ? DIRECTORY_CONTENT_TYPE : contentTypeFor(path);
        if (!directory && isUnsupportedImagePath(path, contentType)) {
          unsupportedImageCount += 1;
          continue;
        }
        const kind = !directory && isImagePath(path, contentType) ? "image" : "file";
        const file = kind === "image" ? await readSelectedFile(path, name) : createEmptyAttachmentFile(name, contentType);
        const id = crypto.randomUUID();
        picked.push({ id, kind, path, name, file: registerLocalAttachment(file, {
          id, path, name, contentType, kind,
        }) });
      }
      if (!insertion.isAvailable()) {
        void frontendLog("INFO", "composer_attachment_selection_discarded", "原输入会话已关闭或禁用，忽略迟到的附件选择", {
          data: { count: picked.length, directory },
        });
        return;
      }
      if (picked.length > 0) await onPicked(picked);
      if (unsupportedImageCount > 0) onError?.(`图片附件仅支持 JPEG 和 PNG，已跳过 ${unsupportedImageCount} 个文件。`);
    } catch (error) {
      if (insertion.isAvailable()) onError?.(userErrorMessage(error, "选择附件失败，请重试"));
      void frontendLog("ERROR", "attachment_picker_failed", "统一附件选择失败", { data: { directory }, error });
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
            className="text-muted-foreground hover:text-foreground hover:bg-muted-foreground/15 size-7 rounded-full"
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
