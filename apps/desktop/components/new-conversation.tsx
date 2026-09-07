"use client";

import { useRef, useState, type FormEvent } from "react";
import { Loader2Icon, SendIcon } from "lucide-react";

import { ComposerControls } from "@/components/composer/composer-controls";
import { ComposerInput } from "@/components/composer/composer-input";
import { ComposerSurface } from "@/components/composer/composer-surface";
import { WorkspacePicker } from "@/components/composer/workspace-picker";
import { CosirMark } from "@/components/cosir-mark";
import { Button } from "@/components/ui/button";
import { createWorkspaceTask, type Workspace, type StartedConversation } from "@/lib/api/workspaces";
import { readStoredSelection, writeStoredSelection } from "@/lib/model-selection-storage";

type NewConversationProps = {
  workspaces: Workspace[];
  selectedWorkspaceId: number | null;
  onWorkspaceChange: (workspaceId: number) => void;
  onWorkspaceCreated: () => Promise<void>;
  onStarted: (conversation: StartedConversation, initialText: string) => void;
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

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedText = text.trim();
    if (!selectedWorkspaceId || !modelReady || !trimmedText || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const selection = readStoredSelection(selectedWorkspaceId);
      if (!selection?.providerId || !selection.modelName) throw new Error("请先选择模型");
      const task = await createWorkspaceTask(selectedWorkspaceId, { text: trimmedText });
      writeStoredSelection(task.task_id, {
        providerId: selection.providerId,
        modelName: selection.modelName,
        reasoningEffort: selection.reasoningEffort ?? null,
      });
      onStarted(task, trimmedText);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "创建对话失败，请重试");
      setSubmitting(false);
    }
  };

  return (
    <div className="mx-auto flex h-full w-full max-w-5xl items-center justify-center px-6">
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
          <ComposerInput
            value={text}
            onTextChange={setText}
            onSubmitText={() => {
              if (!selectedWorkspaceId || !modelReady || !text.trim() || submitting) return;
              formRef.current?.requestSubmit();
            }}
            placeholder="你想让我们在这个工作区中构建什么？"
            className="min-h-24 w-full resize-none bg-transparent px-2 py-1 text-base outline-none"
            disabled={submitting}
            aria-label="新对话内容"
            autoFocus
          />
          <div className="flex flex-wrap items-center justify-between gap-3 pt-3">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <ComposerControls taskId={selectedWorkspaceId ?? undefined} onModelReadyChange={setModelReady} />
            </div>
            <Button type="submit" className="shrink-0 rounded-full" disabled={!selectedWorkspaceId || !modelReady || !text.trim() || submitting}>
              {submitting ? <Loader2Icon className="animate-spin" /> : <SendIcon />}开始对话
            </Button>
          </div>
        </ComposerSurface>
        {error && <p className="text-destructive mt-3 text-center text-sm">{error}</p>}
      </form>
    </div>
  );
}
