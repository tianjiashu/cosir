"use client";

import { useState, type FC } from "react";
import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import { PaperclipIcon } from "lucide-react";

import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { frontendLog } from "@/lib/logging/frontend-log";
import {
  contentTypeFor,
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
};

async function readSelectedFile(path: string, name: string): Promise<File> {
  const bytes = await invoke<number[]>("read_selected_attachment_file", {
    path,
  });
  return new File([new Uint8Array(bytes)], name, { type: contentTypeFor(path) });
}

export const AttachmentPicker: FC<AttachmentPickerProps> = ({
  workspaceRoot,
  onPicked,
  onError,
}) => {
  const [opening, setOpening] = useState(false);

  const choose = async () => {
    if (opening) return;
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
        const file = new File([], name, { type: contentType });
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

  return (
    <TooltipIconButton
      tooltip="添加图片或附件"
      side="bottom"
      variant="ghost"
      size="icon"
      className="text-muted-foreground hover:text-foreground hover:bg-muted-foreground/15 size-7 rounded-full"
      aria-label={opening ? "正在选择附件" : "添加图片或附件"}
      disabled={opening}
      onClick={() => void choose()}
    >
      <PaperclipIcon className="size-4" />
    </TooltipIconButton>
  );
};
