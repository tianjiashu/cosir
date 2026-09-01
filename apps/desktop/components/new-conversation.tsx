"use client";

import { useRef, useState, type FormEvent } from "react";
import { Loader2Icon, SendIcon } from "lucide-react";
import { useRouter } from "next/navigation";

import { ComposerControls } from "@/components/composer/composer-controls";
import { DraftAttachments } from "@/components/composer/draft-attachments";
import { ComposerInput } from "@/components/composer/composer-input";
import { ComposerSurface } from "@/components/composer/composer-surface";
import { WorkspacePicker } from "@/components/composer/workspace-picker";
import { Button } from "@/components/ui/button";
import { createWorkspaceTask, type Workspace } from "@/lib/api/workspaces";

type NewConversationProps = {
  workspaces: Workspace[];
  selectedWorkspaceId: number | null;
  onWorkspaceChange: (workspaceId: number) => void;
  onWorkspaceCreated: () => Promise<void>;
};

export function NewConversation({
  workspaces,
  selectedWorkspaceId,
  onWorkspaceChange,
  onWorkspaceCreated,
}: NewConversationProps) {
  const router = useRouter();
  const formRef = useRef<HTMLFormElement>(null);
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<File[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedText = text.trim();
    if (!selectedWorkspaceId || !trimmedText || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const task = await createWorkspaceTask(selectedWorkspaceId, { text: trimmedText });
      // Keep the first message as a recoverable, task-scoped draft instead of
      // putting the prompt in the URL (which can be truncated or leaked).
      window.sessionStorage.setItem(
        `cosir:pending-initial-message:${task.task_id}`,
        trimmedText,
      );
      router.push(`/tasks/${task.task_id}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "创建对话失败，请重试");
      setSubmitting(false);
    }
  };

  if (workspaces.length === 0) {
    return (
      <div className="mx-auto flex h-full w-full max-w-2xl items-center justify-center px-6">
        <div className="w-full rounded-2xl border border-dashed p-8 text-center">
          <h1 className="text-lg font-semibold">还没有工作区</h1>
          <p className="text-muted-foreground mt-2 text-sm">请先创建工作区，才能创建任务和开始对话。</p>
          <div className="mt-6 flex justify-center">
            <WorkspacePicker
              workspaces={workspaces}
              selectedWorkspaceId={selectedWorkspaceId}
              onWorkspaceChange={onWorkspaceChange}
              onWorkspaceCreated={async () => onWorkspaceCreated()}
              showCreateLabel
            />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto flex h-full w-full max-w-4xl items-center justify-center px-6">
      <form ref={formRef} onSubmit={submit} className="w-full">
        <div className="mb-6 text-center">
          <h1 className="text-3xl font-semibold tracking-tight">开始一个新对话</h1>
          <p className="text-muted-foreground mt-2 text-sm">选择工作区后，任务和后续修改都会归属于它。</p>
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
          <ComposerInput
            value={text}
            onTextChange={setText}
            onSubmitText={() => {
              if (!selectedWorkspaceId || !text.trim() || submitting) return;
              formRef.current?.requestSubmit();
            }}
            placeholder="你想让我们在这个工作区中构建什么？"
            className="min-h-36 w-full resize-none bg-transparent px-2 py-1 text-base outline-none"
            disabled={submitting}
            aria-label="新对话内容"
            autoFocus
          />
          <div className="flex items-center justify-between gap-3 pt-3">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <DraftAttachments files={attachments} onChange={setAttachments} disabled={submitting} />
              <ComposerControls />
            </div>
            <Button type="submit" className="shrink-0 rounded-full" disabled={!selectedWorkspaceId || !text.trim() || submitting}>
              {submitting ? <Loader2Icon className="animate-spin" /> : <SendIcon />}开始对话
            </Button>
          </div>
        </ComposerSurface>
        {error && <p className="text-destructive mt-3 text-center text-sm">{error}</p>}
      </form>
    </div>
  );
}
