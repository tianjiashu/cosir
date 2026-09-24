"use client";

import { useState, type FC } from "react";
import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import { FilePlusIcon, FolderOpenIcon, PaperclipIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { frontendLog } from "@/lib/logging/frontend-log";
import {
  contentTypeFor,
  DIRECTORY_CONTENT_TYPE,
  fileName,
  isImagePath,
} from "@/components/composer/attachment-policy";
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

  const chooseFiles = async () => {
    if (opening || disabled) return;
    setOpening(true);
    try {
      const selected = await open({
        multiple: true,
        directory: false,
        defaultPath: workspaceRoot,
        title: "选择图片或参考附件",
      });
      const paths = selected === null ? [] : Array.isArray(selected) ? selected : [selected];
      if (paths.length === 0) return;

      const picked: PickedComposerAttachment[] = [];
      for (const selectedPath of paths) {
        const path = await invoke<string>("resolve_selected_attachment_path", { path: selectedPath });
        const name = fileName(path);
        const contentType = contentTypeFor(path);
        if (isImagePath(path, contentType)) {
          const file = await readSelectedFile(path, name);
          const id = crypto.randomUUID();
          picked.push({ id, kind: "image", path, name, file: registerLocalAttachment(file, {
            id, path, name, contentType, kind: "image",
          }) });
          continue;
        }
        const file = createEmptyAttachmentFile(name, contentType);
        const id = crypto.randomUUID();
        picked.push({ id, kind: "file", path, name, file: registerLocalAttachment(file, {
          id, path, name, contentType, kind: "file",
        }) });
      }
      if (picked.length > 0) await onPicked(picked);
    } catch (error) {
      const message = error instanceof Error ? error.message : "选择附件失败";
      onError?.(message);
      await frontendLog("ERROR", "attachment_picker_failed", "统一附件选择失败", {
        data: { selectedCount: 0 },
        error,
      });
    } finally {
      setOpening(false);
    }
  };

  const chooseDirectory = async () => {
    if (opening || disabled) return;
    setOpening(true);
    try {
      const selected = await open({
        multiple: false,
        directory: true,
        defaultPath: workspaceRoot,
        title: "选择参考文件夹",
      });
      if (typeof selected !== "string") return;

      const path = await invoke<string>("resolve_selected_attachment_path", { path: selected });
      const name = fileName(path);
      const id = crypto.randomUUID();
      const file = createEmptyAttachmentFile(name, DIRECTORY_CONTENT_TYPE);
      await onPicked([{
        id,
        kind: "file",
        path,
        name,
        file: registerLocalAttachment(file, {
          id,
          path,
          name,
          contentType: DIRECTORY_CONTENT_TYPE,
          kind: "file",
        }),
      }]);
    } catch (error) {
      const message = error instanceof Error ? error.message : "选择文件夹失败";
      onError?.(message);
      await frontendLog("ERROR", "attachment_directory_picker_failed", "文件夹附件选择失败", {
        data: { selectedCount: 0 },
        error,
      });
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
          onClick={() => void chooseFiles()}
        >
          <FilePlusIcon className="text-muted-foreground size-4" />
          <span>选择文件或图片</span>
        </button>
        <button
          type="button"
          className="hover:bg-muted flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
          onClick={() => void chooseDirectory()}
        >
          <FolderOpenIcon className="text-muted-foreground size-4" />
          <span>选择文件夹</span>
        </button>
      </PopoverContent>
    </Popover>
  );
};
