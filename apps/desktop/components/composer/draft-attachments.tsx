"use client";

import { useRef, type ChangeEvent } from "react";
import { FileIcon, PlusIcon, XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

type DraftAttachmentsProps = {
  files: File[];
  onChange: (files: File[]) => void;
  disabled?: boolean;
};

export function DraftAttachments({ files, onChange, disabled = false }: DraftAttachmentsProps) {
  const inputRef = useRef<HTMLInputElement>(null);

  const addFiles = (event: ChangeEvent<HTMLInputElement>) => {
    const nextFiles = Array.from(event.target.files ?? []);
    if (nextFiles.length > 0) onChange([...files, ...nextFiles]);
    event.target.value = "";
  };

  return (
    <div className="flex min-w-0 items-center gap-2">
      <input ref={inputRef} type="file" multiple className="sr-only" onChange={addFiles} disabled={disabled} />
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="text-muted-foreground hover:text-foreground hover:bg-muted size-8 rounded-full"
        onClick={() => inputRef.current?.click()}
        disabled={disabled}
        aria-label="添加附件"
        title="添加附件"
      >
        <PlusIcon />
      </Button>
      {files.length > 0 && (
        <div className="flex min-w-0 items-center gap-1.5 overflow-x-auto">
          {files.map((file, index) => (
            <span key={`${file.name}-${file.lastModified}-${index}`} className="bg-muted text-muted-foreground inline-flex max-w-44 items-center gap-1 rounded-md px-2 py-1 text-xs">
              <FileIcon className="size-3.5 shrink-0" />
              <span className="truncate" title={file.name}>{file.name}</span>
              <button type="button" className="hover:text-foreground shrink-0" onClick={() => onChange(files.filter((_, fileIndex) => fileIndex !== index))} aria-label={`移除附件 ${file.name}`}>
                <XIcon className="size-3.5" />
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
