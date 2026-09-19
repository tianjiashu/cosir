"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { FileTextIcon, Loader2Icon, SendIcon, XIcon } from "lucide-react";

import { ComposerControls } from "@/components/composer/composer-controls";
import {
  InlineAttachmentInput,
  InlineAttachmentInsertionProvider,
  useInlineAttachmentInsertion,
  type InlineFileAttachment,
} from "@/components/composer/inline-attachment-input";
import { ComposerSurface } from "@/components/composer/composer-surface";
import { WorkspacePicker } from "@/components/composer/workspace-picker";
import { CosirMark } from "@/components/cosir-mark";
import { Button } from "@/components/ui/button";
import { createWorkspaceTask, type Workspace, type StartedConversation } from "@/lib/api/workspaces";
import { readStoredSelection, writeStoredSelection } from "@/lib/model-selection-storage";
import { AttachmentPicker, type PickedComposerAttachment } from "@/components/composer/attachment-picker";
import { ImageAttachmentCard } from "@/components/composer/image-attachment-card";
import {
  Dialog,
  DialogContent,
  DialogTitle,
} from "@/components/ui/dialog";

export type InitialConversationAttachment = PickedComposerAttachment;

function NewConversationAttachmentChip({ attachment, onRemove }: {
  attachment: PickedComposerAttachment;
  onRemove: () => void;
}) {
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);

  useEffect(() => {
    if (attachment.kind !== "image") return;
    const url = URL.createObjectURL(attachment.file);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [attachment.file, attachment.kind]);

  if (attachment.kind === "image") {
    return (
      <>
        <ImageAttachmentCard
          src={previewUrl}
          name={attachment.name}
          onPreview={() => setPreviewOpen(true)}
          onRemove={onRemove}
        />
        <Dialog open={previewOpen} onOpenChange={setPreviewOpen}>
          <DialogContent className="p-2 sm:max-w-3xl">
            <DialogTitle className="sr-only">预览图片：{attachment.name}</DialogTitle>
            <div className="flex max-h-[80dvh] w-full items-center justify-center overflow-hidden rounded-sm bg-muted">
              {previewUrl && <img src={previewUrl} alt={attachment.name} className="max-h-[80dvh] max-w-full object-contain" />}
            </div>
          </DialogContent>
        </Dialog>
      </>
    );
  }

  return (
    <div className="bg-muted flex max-w-full items-center gap-1.5 rounded-md px-2 py-1 text-xs">
      <FileTextIcon className="size-3.5 shrink-0" />
      <span className="max-w-56 truncate">{attachment.name}</span>
      <button
        type="button"
        className="text-muted-foreground hover:text-foreground rounded-sm"
        aria-label={`移除附件 ${attachment.name}`}
        onClick={onRemove}
      >
        <XIcon className="size-3.5" />
      </button>
    </div>
  );
}

function NewConversationAttachmentPicker({
  workspaceRoot,
  existingPaths,
  onPicked,
  onError,
}: {
  workspaceRoot?: string;
  existingPaths: ReadonlySet<string>;
  onPicked: (attachments: PickedComposerAttachment[]) => void | Promise<void>;
  onError: (message: string) => void;
}) {
  const insertion = useInlineAttachmentInsertion();
  return (
    <AttachmentPicker
      workspaceRoot={workspaceRoot}
      onPicked={async (picked) => {
        const accepted = picked.filter((attachment) => !existingPaths.has(attachment.path.toLowerCase()));
        if (accepted.length === 0) return;
        await onPicked(accepted);
        insertion.insert(accepted
          .filter((attachment) => attachment.kind === "file")
          .map((attachment) => ({ id: attachment.id, name: attachment.name, kind: "file" as const })));
      }}
      onError={onError}
    />
  );
}

type NewConversationProps = {
  workspaces: Workspace[];
  selectedWorkspaceId: number | null;
  onWorkspaceChange: (workspaceId: number) => void;
  onWorkspaceCreated: () => Promise<void>;
  onStarted: (
    conversation: StartedConversation,
    initialText: string,
    attachments: InitialConversationAttachment[],
  ) => void;
};

export function NewConversation({
  workspaces,
  selectedWorkspaceId,
  onWorkspaceChange,
  onWorkspaceCreated,
  onStarted,
}: NewConversationProps) {
  const formRef = useRef<HTMLFormElement>(null);
  const [text, setText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [modelReady, setModelReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<PickedComposerAttachment[]>([]);
  const selectedWorkspace = workspaces.find((workspace) => workspace.workspace_id === selectedWorkspaceId);
  const fileAttachments: InlineFileAttachment[] = attachments
    .filter((attachment) => attachment.kind === "file")
    .map((attachment) => ({
      id: attachment.id,
      name: attachment.name,
    }));
  const hasDraft = Boolean(text.trim() || attachments.length > 0);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedText = text.trim();
    if (!selectedWorkspaceId || !modelReady || !hasDraft || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const selection = readStoredSelection({ kind: "workspace", id: selectedWorkspaceId });
      if (!selection?.providerId || !selection.modelName) throw new Error("请先选择模型");
      const task = await createWorkspaceTask(selectedWorkspaceId, { text: trimmedText });
      writeStoredSelection({ kind: "task", id: task.task_id }, {
        providerId: selection.providerId,
        modelName: selection.modelName,
        reasoningEffort: selection.reasoningEffort ?? null,
      });
      onStarted(
        task,
        trimmedText,
        attachments,
      );
      setSubmitting(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "创建对话失败，请重试");
      setSubmitting(false);
    }
  };

  return (
    <div className="mx-auto flex h-full w-full max-w-5xl items-center justify-center px-6">
      <InlineAttachmentInsertionProvider>
      <form ref={formRef} onSubmit={submit} className="w-full">
        <div className="mb-8 flex flex-col items-center gap-3 text-center">
          <CosirMark className="text-foreground size-16" />
          <h1 className="text-5xl font-semibold tracking-[0.12em]">COSIR</h1>
        </div>
        <ComposerSurface
          className="bg-card focus-within:ring-ring/20 shadow-sm focus-within:ring-2"
          style={{
            ["--composer-radius" as string]: "1.5rem",
            ["--composer-padding" as string]: "8px",
            ["--composer-bg" as string]: "var(--color-card)",
          }}
        >
          <div className="border-border/50 mb-1 flex min-w-0 items-center gap-2 border-b px-2 pb-2">
            <WorkspacePicker
              workspaces={workspaces}
              selectedWorkspaceId={selectedWorkspaceId}
              onWorkspaceChange={onWorkspaceChange}
              onWorkspaceCreated={async () => onWorkspaceCreated()}
            />
          </div>
          {attachments.some((attachment) => attachment.kind === "image") && (
            <div className="flex h-16 max-h-16 flex-nowrap gap-2 overflow-x-auto px-2" aria-label="待发送附件">
              {attachments.filter((attachment) => attachment.kind === "image").map((attachment) => (
                <NewConversationAttachmentChip
                  key={attachment.path}
                  attachment={attachment}
                  onRemove={() => setAttachments((current) => current.filter((item) => item.path !== attachment.path))}
                />
              ))}
            </div>
          )}
          <InlineAttachmentInput
            value={text}
            onChange={setText}
            onSubmit={() => {
              if (!selectedWorkspaceId || !modelReady || !hasDraft || submitting) return;
              formRef.current?.requestSubmit();
            }}
            attachments={fileAttachments}
            onRemoveAttachment={(fileId) => {
              setAttachments((current) => current.filter((attachment) => attachment.id !== fileId));
            }}
            placeholder="你想让我们在这个工作区中构建什么？"
            className="min-h-24 w-full resize-none bg-transparent px-2 py-1 text-base outline-none"
            disabled={submitting}
            aria-label="新对话内容"
            autoFocus
          />
          <div className="flex flex-wrap items-center justify-between gap-3 pt-3">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <ComposerControls
                scope={selectedWorkspaceId ? { kind: "workspace", id: selectedWorkspaceId } : undefined}
                onModelReadyChange={setModelReady}
              />
            </div>
            <div className="flex items-center gap-1">
              <NewConversationAttachmentPicker
                workspaceRoot={selectedWorkspace?.root_path}
                existingPaths={new Set(attachments.map((attachment) => attachment.path.toLowerCase()))}
                onPicked={async (picked) => {
                  setAttachments((current) => {
                    const existing = new Set(current.map((attachment) => attachment.path.toLowerCase()));
                    return [...current, ...picked.filter((attachment) => !existing.has(attachment.path.toLowerCase()))];
                  });
                }}
                onError={setError}
              />
              <Button type="submit" className="shrink-0 rounded-full" disabled={!selectedWorkspaceId || !modelReady || !hasDraft || submitting}>
              {submitting ? <Loader2Icon className="animate-spin" /> : <SendIcon />}开始对话
              </Button>
            </div>
          </div>
        </ComposerSurface>
        {error && <p className="text-destructive mt-3 text-center text-sm">{error}</p>}
      </form>
      </InlineAttachmentInsertionProvider>
    </div>
  );
}
