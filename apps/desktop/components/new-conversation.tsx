"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { FileTextIcon, Loader2Icon, SendIcon, XIcon } from "lucide-react";

import { ComposerControls } from "@/components/composer/composer-controls";
import { ComposerEditor, COMPOSER_EDITOR_CLASS_NAME } from "@/components/composer/composer-editor";
import { ComposerFeedback, ComposerFeedbackProvider, useComposerFeedback } from "@/components/composer/composer-feedback";
import { classifyComposerExternalFiles, type ComposerExternalFileResult } from "@/components/composer/composer-external-files";
import { insertAttachments, useComposerInsertion, ComposerInsertionProvider } from "@/components/composer/composer-insertion-context";
import type { InlineComposerAttachment } from "@/components/composer/composer-document";
import { ComposerPromptToolbar } from "@/components/composer/composer-prompt-toolbar";
import { ComposerSurface } from "@/components/composer/composer-surface";
import { WorkspacePicker } from "@/components/composer/workspace-picker";
import { CosirMark } from "@/components/cosir-mark";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { createWorkspaceTask, deleteTask, type Workspace, type StartedConversation } from "@/lib/api/workspaces";
import { getToolGroups, type ToolGroupCatalog } from "@/lib/api/tools";
import { readStoredSelection, writeStoredSelection } from "@/lib/model-selection-storage";
import { removeStoredToolGroupSelection, writeStoredToolGroupSelection } from "@/lib/tool-group-selection-storage";
import { AttachmentPicker, type PickedComposerAttachment } from "@/components/composer/attachment-picker";
import { ImageAttachmentCard } from "@/components/composer/image-attachment-card";
import { registerLocalAttachment } from "@/lib/assistant/attachments/local-attachment-registry";
import { submitAssistantTransport, wasCommandPossiblyAccepted } from "@/lib/assistant/submit-assistant-transport";
import { isSupportedImageFile } from "@/lib/assistant/attachments/attachment-upload";
import { frontendLog } from "@/lib/logging/frontend-log";
import {
  Dialog,
  DialogContent,
  DialogTitle,
} from "@/components/ui/dialog";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { newTraceId } from "@/lib/trace";

export type InitialConversationAttachment = PickedComposerAttachment;

function clipboardAttachment(file: File): PickedComposerAttachment {
  const id = crypto.randomUUID();
  const kind = isSupportedImageFile(file) ? "image" : "file";
  const name = file.name || (kind === "image" ? `粘贴图片-${id}.png` : `粘贴附件-${id}`);
  const path = `clipboard:${id}`;
  const namedFile = file.name ? file : new File([file], name, { type: file.type, lastModified: file.lastModified });
  return {
    id,
    kind,
    path,
    name,
    file: registerLocalAttachment(namedFile, {
      id,
      path,
      name,
      contentType: namedFile.type,
      kind,
    }),
  };
}

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
}: {
  workspaceRoot?: string;
  existingPaths: ReadonlySet<string>;
  onPicked: (attachments: PickedComposerAttachment[]) => void | Promise<void>;
}) {
  const insertion = useComposerInsertion();
  const feedback = useComposerFeedback();
  return (
    <AttachmentPicker
      workspaceRoot={workspaceRoot}
      onPicked={async (picked) => {
        const accepted = picked.filter((attachment) => !existingPaths.has(attachment.path.toLowerCase()));
        if (accepted.length === 0) return;
        await onPicked(accepted);
        insertAttachments(insertion.dispatch, accepted
          .filter((attachment) => attachment.kind === "file")
          .map((attachment) => ({ id: attachment.id, name: attachment.name, kind: "file" as const })));
      }}
      onError={feedback.show}
    />
  );
}

type NewConversationProps = {
  workspaces: Workspace[];
  selectedWorkspaceId: number | null;
  onWorkspaceChange: (workspaceId: number) => void;
  onWorkspaceCreated: () => Promise<void>;
  /** 首条命令已被后端受理且用户仍停留在本页：由宿主导航到新 Task。 */
  onStarted: (
    conversation: StartedConversation,
    disabledToolGroups: string[],
  ) => void;
  /** 新 Task 被保留但不导航（用户已离开本页，或受理结果未知）：宿主只需刷新侧栏。 */
  onTaskListChanged: () => void;
};

const EMPTY_TOOL_GROUPS: ToolGroupCatalog[] = [];

export function NewConversation({
  workspaces,
  selectedWorkspaceId,
  onWorkspaceChange,
  onWorkspaceCreated,
  onStarted,
  onTaskListChanged,
}: NewConversationProps) {
  const formRef = useRef<HTMLFormElement>(null);
  const location = useLocation();
  // 提交跨越多个 await：完成时用 ref 判断用户是否仍在本页，避免把已离开的用户拽回新 Task。
  const pathnameRef = useRef(location.pathname);
  pathnameRef.current = location.pathname;
  const mountedRef = useRef(true);
  const [text, setText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [modelReady, setModelReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<PickedComposerAttachment[]>([]);
  const [selectedToolGroups, setSelectedToolGroups] = useState<string[]>([]);
  const toolGroupsQuery = useQuery({
    queryKey: queryKeys.toolGroups(),
    queryFn: ({ signal }) => getToolGroups({ signal }),
  });
  const toolGroups = toolGroupsQuery.data?.groups ?? EMPTY_TOOL_GROUPS;
  const toolGroupsError = toolGroupsQuery.isError
    ? userErrorMessage(toolGroupsQuery.error, "工具组加载失败")
    : null;
  const selectedWorkspace = workspaces.find((workspace) => workspace.workspace_id === selectedWorkspaceId);
  const fileAttachments: InlineComposerAttachment[] = attachments
    .filter((attachment) => attachment.kind === "file")
    .map((attachment) => ({
      id: attachment.id,
      name: attachment.name,
    }));
  const hasDraft = Boolean(text.trim() || attachments.length > 0);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  /**
   * 创建 provisional Task 并提交首条命令。
   *
   * 生命周期约束：
   * - 只在用户仍停留在本页时导航；用户中途离开（切到其它 Task、组件卸载）只刷新侧栏并 toast，
   *   不能把用户拽回新 Task。
   * - 只有确定命令未被受理（请求未发出或 HTTP 明确拒绝）才删除 provisional Task；网络层结果未知
   *   或已越过 accepted barrier 时删除会丢掉可能正在运行的对话，见 wasCommandPossiblyAccepted。
   */
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedText = text.trim();
    if (!selectedWorkspaceId || !modelReady || !hasDraft || submitting) return;
    setSubmitting(true);
    setError(null);
    const sendAttachments = [...attachments];
    const creationCommandId = crypto.randomUUID();
    const traceId = newTraceId();
    const submittedPathname = pathnameRef.current;
    const stillHere = () => mountedRef.current && pathnameRef.current === submittedPathname;
    let provisionalTask: StartedConversation | null = null;
    try {
      const selection = readStoredSelection({ kind: "workspace", id: selectedWorkspaceId });
      if (!selection?.modelConfigId) throw new UserFacingError("请先选择模型");
      const task = await createWorkspaceTask(selectedWorkspaceId, {
        text: trimmedText,
        creationCommandId,
      });
      provisionalTask = task;
      writeStoredToolGroupSelection(task.task_id, selectedToolGroups);
      await submitAssistantTransport({
        taskId: task.task_id,
        workspaceId: selectedWorkspaceId,
        commandId: creationCommandId,
        text: trimmedText,
        imageAttachments: sendAttachments
          .filter((attachment) => attachment.kind === "image")
          .map((attachment) => ({ file: attachment.file, name: attachment.name })),
        banTools: toolGroups
          .filter(({ group }) => selectedToolGroups.includes(group))
          .flatMap(({ tools }) => tools.map(({ name }) => name)),
        modelConfigId: selection.modelConfigId,
        reasoningEffort: selection.reasoningEffort ?? null,
        traceId,
      });
      writeStoredSelection({ kind: "task", id: task.task_id }, {
        modelConfigId: selection.modelConfigId,
        reasoningEffort: selection.reasoningEffort ?? null,
      });
      if (stillHere()) {
        onStarted(task, selectedToolGroups);
      } else {
        void frontendLog("INFO", "new_conversation_started_after_leave", "用户已离开新对话页，首条消息受理后不再导航", {
          traceId,
          data: { taskId: task.task_id, creationCommandId },
        });
        onTaskListChanged();
        notify.success("新对话已开始运行", { description: "可在侧栏中打开查看" });
      }
    } catch (cause) {
      const possiblyAccepted = wasCommandPossiblyAccepted(cause);
      if (provisionalTask && !possiblyAccepted) {
        try {
          await deleteTask(provisionalTask.task_id, creationCommandId);
          removeStoredToolGroupSelection(provisionalTask.task_id);
        } catch (cleanupError) {
          void frontendLog("ERROR", "provisional_task_cleanup_failed", "provisional Task 清理失败", {
            traceId,
            data: { taskId: provisionalTask.task_id, creationCommandId },
            error: cleanupError,
          });
        }
      }
      if (provisionalTask && possiblyAccepted) onTaskListChanged();
      void frontendLog("ERROR", "new_conversation_submit_failed", "新对话首条消息发送失败", {
        traceId,
        data: { taskId: provisionalTask?.task_id ?? null, creationCommandId, possiblyAccepted },
        error: cause,
      });
      if (stillHere()) setError(userErrorMessage(cause, "创建对话失败，请重试"));
      else notify.error("创建对话失败", { error: cause, fallback: "创建对话失败，请重试" });
    } finally {
      if (mountedRef.current) setSubmitting(false);
    }
  };

  return (
    <div className="mx-auto flex h-full w-full max-w-5xl items-center justify-center px-6">
      <ComposerInsertionProvider>
      <form ref={formRef} onSubmit={submit} className="w-full">
        <div className="mb-8 flex flex-col items-center gap-3 text-center">
          <CosirMark className="text-foreground size-16" />
          <h1 className="text-5xl font-semibold tracking-[0.12em]">COSIR</h1>
        </div>
        <ComposerSurface>
          <ComposerFeedbackProvider>
          <div className="border-border/50 mb-1 flex min-w-0 items-center gap-2 border-b px-2 pb-2">
            <WorkspacePicker
              workspaces={workspaces}
              selectedWorkspaceId={selectedWorkspaceId}
              onWorkspaceChange={onWorkspaceChange}
              onWorkspaceCreated={async () => onWorkspaceCreated()}
            />
          </div>
          <div className="min-w-0 px-2">
            <ComposerPromptToolbar
              toolGroups={toolGroups}
              selectedToolGroups={selectedToolGroups}
              onSelectedToolGroupsChange={setSelectedToolGroups}
              toolGroupsLoading={toolGroupsQuery.isPending}
              toolGroupsError={toolGroupsError}
              disabled={submitting}
            />
          </div>
          {attachments.some((attachment) => attachment.kind === "image") && (
            <div className="flex h-16 min-h-0 max-h-16 min-w-0 flex-nowrap gap-2 overflow-x-auto overflow-y-hidden px-2" aria-label="待发送附件">
              {attachments.filter((attachment) => attachment.kind === "image").map((attachment) => (
                <NewConversationAttachmentChip
                  key={attachment.path}
                  attachment={attachment}
                  onRemove={() => setAttachments((current) => current.filter((item) => item.path !== attachment.path))}
                />
              ))}
            </div>
          )}
          <ComposerEditor
            value={text}
            onChange={setText}
            onSubmit={() => {
              if (!selectedWorkspaceId || !modelReady || !hasDraft || submitting) return;
              formRef.current?.requestSubmit();
            }}
            attachments={fileAttachments}
            onExternalFiles={(files): ComposerExternalFileResult<InlineComposerAttachment> => {
              const { accepted, problems } = classifyComposerExternalFiles(files);
              const picked = accepted.map(clipboardAttachment);
              setAttachments((current) => {
                const existing = new Set(current.map((attachment) => attachment.path.toLowerCase()));
                return [...current, ...picked.filter((attachment) => !existing.has(attachment.path.toLowerCase()))];
              });
              return {
                attachments: picked
                  .filter((attachment) => attachment.kind === "file")
                  .map((attachment) => ({ id: attachment.id, name: attachment.name, kind: "file" as const })),
                problems,
              };
            }}
            onRemoveAttachment={(fileId) => {
              setAttachments((current) => current.filter((attachment) => attachment.id !== fileId));
            }}
            placeholder="你想让我们在这个工作区中构建什么？"
            className={COMPOSER_EDITOR_CLASS_NAME}
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
              />
              <Button type="submit" className="shrink-0 rounded-full" disabled={!selectedWorkspaceId || !modelReady || !hasDraft || submitting}>
              {submitting ? <Loader2Icon className="animate-spin" /> : <SendIcon />}开始对话
              </Button>
            </div>
          </div>
          <ComposerFeedback />
          </ComposerFeedbackProvider>
        </ComposerSurface>
        <Dialog open={Boolean(error)} onOpenChange={(open) => { if (!open) setError(null); }}>
          <DialogContent>
            <DialogTitle>创建对话失败</DialogTitle>
            <Alert tone="destructive">{error}</Alert>
          </DialogContent>
        </Dialog>
      </form>
      </ComposerInsertionProvider>
    </div>
  );
}
