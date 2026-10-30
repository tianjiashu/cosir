"use client";

import { create } from "zustand";
import type { ComponentProps, SetStateAction } from "react";

import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Loader2Icon, SendIcon } from "lucide-react";

import { ComposerControls } from "@/components/composer/composer-controls";
import { ComposerEditor, COMPOSER_EDITOR_CLASS_NAME, COMPOSER_PLACEHOLDER } from "@/components/composer/composer-editor";
import { ComposerFeedback, ComposerFeedbackProvider, useComposerFeedback } from "@/components/composer/composer-feedback";
import { ComposerInsertionProvider } from "@/components/composer/composer-insertion-context";
import { useComposerFiles } from "@/components/composer/use-composer-files";
import { localFileTokenIds } from "@/lib/assistant/attachments/local-file-token";
import { ComposerPromptToolbar } from "@/components/composer/composer-prompt-toolbar";
import { ComposerSurface } from "@/components/composer/composer-surface";
import { useCreateWorkspaceFromFolder, WorkspacePicker } from "@/components/composer/workspace-picker";
import { CosirMark } from "@/components/cosir-mark";
import { FirstRunChecklist } from "@/components/onboarding/first-run-checklist";
import { modelSetupState, shouldShowFirstRunChecklist, startBlockedReason } from "@/components/onboarding/start-readiness";
import { Button } from "@/components/ui/button";
import { createWorkspaceTask, deleteTask, type Workspace, type StartedConversation } from "@/lib/api/workspaces";
import { getToolGroups, type ToolGroupCatalog } from "@/lib/api/tools";
import { useModelCatalog } from "@/lib/model-catalog";
import { cn } from "@/lib/utils";
import { readStoredSelection, writeStoredSelection } from "@/lib/model-selection-storage";
import { AttachmentPicker, type PickedComposerAttachment } from "@/components/composer/attachment-picker";
import { ImageAttachmentCard, ImageAttachmentStrip } from "@/components/composer/image-attachment-card";
import { getLocalAttachment, getLocalAttachmentById, registerLocalAttachment } from "@/lib/assistant/attachments/local-attachment-registry";
import { submitNewConversationMessage, wasCommandPossiblyAccepted } from "@/lib/assistant/submit-new-conversation-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { showErrorDialog } from "@/lib/confirm/confirm-store";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { newTraceId } from "@/lib/trace";

export type InitialConversationAttachment = PickedComposerAttachment;

type ImageDraftAttachment = Extract<PickedComposerAttachment, { kind: "image" }>;

function imageAttachment(file: File): ImageDraftAttachment {
  const local = getLocalAttachment(file);
  if (local) return { ...local, kind: "image", file };
  const id = crypto.randomUUID();
  const kind = "image";
  const name = file.name || `粘贴图片-${id}.png`;
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
  attachment: ImageDraftAttachment;
  onRemove: () => void;
}) {
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  useEffect(() => {
    const url = URL.createObjectURL(attachment.file);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [attachment.file]);

  return <ImageAttachmentCard src={previewUrl} name={attachment.name} onRemove={onRemove} />;
}

function NewConversationInput({ addImage, ...props }: Omit<ComponentProps<typeof ComposerEditor>, "onExternalFiles"> & {
  addImage: (file: File) => void;
}) {
  const addFiles = useComposerFiles(addImage);
  return <ComposerEditor {...props} onExternalFiles={addFiles} />;
}

function NewConversationAttachmentPicker({ workspaceRoot, existingPaths, addImage, disabled }: {
  workspaceRoot?: string;
  existingPaths: ReadonlySet<string>;
  addImage: (file: File) => void;
  disabled: boolean;
}) {
  const addFiles = useComposerFiles(addImage);
  const feedback = useComposerFeedback();
  return (
    <AttachmentPicker
      workspaceRoot={workspaceRoot}
      disabled={disabled}
      onPicked={(picked) => addFiles(picked.filter((attachment) => !existingPaths.has(attachment.path.toLowerCase())).map(({ file }) => file))}
      onError={feedback.show}
    />
  );
}

type NewConversationProps = {
  workspaces: Workspace[];
  /**
   * 工作区目录是否仍在首次加载：加载完成前 workspaces 为空并不代表用户没有工作区，
   * 据此避免首帧闪出「添加工作区」引导。
   */
  workspacesLoading: boolean;
  selectedWorkspaceId: number | null;
  onWorkspaceChange: (workspaceId: number) => void;
  onWorkspaceCreated: () => Promise<void>;
  /** 首条命令已被后端受理且用户仍停留在本页：由宿主导航到新 Task。 */
  onStarted: (conversation: StartedConversation) => void;
  /** 新 Task 被保留但不导航（用户已离开本页，或受理结果未知）：宿主只需刷新侧栏。 */
  onTaskListChanged: () => void;
};

type NewTaskDraft = { text: string; attachments: ImageDraftAttachment[] };
const emptyDraft: NewTaskDraft = { text: "", attachments: [] };
const useNewTaskDrafts = create<{ drafts: Record<number, NewTaskDraft>; update: (id: number, update: (draft: NewTaskDraft) => NewTaskDraft) => void }>(set => ({ drafts: {}, update: (id, update) => set(state => ({ drafts: { ...state.drafts, [id]: update(state.drafts[id] ?? emptyDraft) } })) }));

const EMPTY_TOOL_GROUPS: ToolGroupCatalog[] = [];

export function NewConversation({
  workspaces,
  workspacesLoading,
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
  const draftId = selectedWorkspaceId ?? 0;
  const currentDraftId = useRef(draftId);
  currentDraftId.current = draftId;
  const draft = useNewTaskDrafts(state => state.drafts[draftId] ?? emptyDraft);
  const updateDraft = useNewTaskDrafts(state => state.update);
  const text = draft.text;
  const setText = (text: string) => updateDraft(draftId, current => ({ ...current, text }));
  const [submitting, setSubmitting] = useState(false);
  const [modelReady, setModelReady] = useState(false);
  const attachments = draft.attachments;
  const setAttachments = (value: SetStateAction<ImageDraftAttachment[]>) => updateDraft(draftId, current => ({ ...current, attachments: typeof value === "function" ? value(current.attachments) : value }));
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
  const addImage = (file: File) => setAttachments((current) => [...current, imageAttachment(file)]);
  const existingPaths = new Set([
    ...attachments.map((attachment) => attachment.path.toLowerCase()),
    ...localFileTokenIds(text).flatMap((id) => {
      const path = getLocalAttachmentById(id)?.path;
      return path ? [path.toLowerCase()] : [];
    }),
  ]);
  const hasDraft = Boolean(text.trim() || attachments.length > 0);
  const modelState = modelSetupState(useModelCatalog());
  const workspaceCreation = useCreateWorkspaceFromFolder({ onWorkspaceCreated, onWorkspaceChange });
  const hasWorkspaces = workspaces.length > 0;
  const showChecklist = shouldShowFirstRunChecklist({ workspacesLoading, hasWorkspaces, modelState });
  const canStart = Boolean(selectedWorkspaceId) && modelReady && hasDraft && !submitting;
  const blockedReasonId = useId();
  const blockedReason = startBlockedReason({ hasWorkspaces, selectedWorkspaceId, modelState, modelReady });

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
    if (!canStart || !selectedWorkspaceId) return;
    setSubmitting(true);
    const sendAttachments = [...attachments];
    const creationCommandId = crypto.randomUUID();
    const traceId = newTraceId();
    const submittedPathname = pathnameRef.current;
    const stillHere = () => mountedRef.current && pathnameRef.current === submittedPathname && currentDraftId.current === draftId;
    let provisionalTask: StartedConversation | null = null;
    try {
      const selection = readStoredSelection({ kind: "workspace", id: selectedWorkspaceId });
      if (!selection?.modelConfigId) throw new UserFacingError("请先选择模型");
      const task = await createWorkspaceTask(selectedWorkspaceId, {
        text: trimmedText,
        creationCommandId,
      });
      provisionalTask = task;
      await submitNewConversationMessage({
        taskId: task.task_id,
        workspaceId: selectedWorkspaceId,
        text: trimmedText,
        imageAttachments: sendAttachments.map((attachment) => ({ file: attachment.file, name: attachment.name })),
        // 首条消息冻结进 Run 的 launch；任务页的工具组默认值从快照取，不另存本地。
        disabledToolGroups: selectedToolGroups,
        modelConfigId: selection.modelConfigId,
        reasoningEffort: selection.reasoningEffort ?? null,
        traceId,
      });
      writeStoredSelection({ kind: "task", id: task.task_id }, {
        modelConfigId: selection.modelConfigId,
        reasoningEffort: selection.reasoningEffort ?? null,
      });
      updateDraft(draftId, () => emptyDraft);
      if (stillHere()) {
        onStarted(task);
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
          await deleteTask(provisionalTask.task_id, { creationCommandId });
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
      if (stillHere()) void showErrorDialog({ title: "创建对话失败", message: userErrorMessage(cause, "创建对话失败，请重试") });
      else notify.error("创建对话失败", { error: cause, fallback: "创建对话失败，请重试" });
    } finally {
      if (mountedRef.current) setSubmitting(false);
    }
  };

  return (
    // 上下留白 2:3，把视觉中心放在略高于正中处；内容超高（首次引导展开）时整页滚动。
    <div className="mx-auto grid h-full w-full max-w-conversation grid-rows-[2fr_auto_3fr] overflow-y-auto px-6">
      <ComposerInsertionProvider key={draftId}>
      <form ref={formRef} onSubmit={submit} className="row-start-2 w-full">
        <div className="mb-6 flex flex-col items-center gap-4 text-center">
          <CosirMark className="text-foreground size-11" />
          <h1 className="max-w-full truncate text-2xl font-medium" title={selectedWorkspace?.name}>
            {selectedWorkspace ? `在 ${selectedWorkspace.name} 里想做什么？` : "想做什么？"}
          </h1>
        </div>
        {showChecklist && (
          <FirstRunChecklist
            hasWorkspace={hasWorkspaces}
            modelState={modelState}
            onAddWorkspace={() => void workspaceCreation.create()}
            addingWorkspace={workspaceCreation.creating}
            addWorkspaceError={workspaceCreation.error}
          />
        )}
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
          <ImageAttachmentStrip>
            {attachments.map((attachment) => (
              <NewConversationAttachmentChip
                key={attachment.path}
                attachment={attachment}
                onRemove={() => setAttachments((current) => current.filter((item) => item.path !== attachment.path))}
              />
            ))}
          </ImageAttachmentStrip>
          <NewConversationInput
            addImage={addImage}
            value={text}
            onChange={setText}
            onSubmit={() => {
              if (canStart) formRef.current?.requestSubmit();
            }}
            placeholder={COMPOSER_PLACEHOLDER}
            className={cn(COMPOSER_EDITOR_CLASS_NAME, "min-h-20")}
            disabled={submitting}
            aria-label="新对话内容"
            autoFocus
          />
          <div className="flex flex-wrap items-center justify-between gap-3 pt-3">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <ComposerPromptToolbar
                toolGroups={toolGroups}
                selectedToolGroups={selectedToolGroups}
                onSelectedToolGroupsChange={setSelectedToolGroups}
                toolGroupsLoading={toolGroupsQuery.isPending}
                toolGroupsError={toolGroupsError}
                disabled={submitting}
              />
              <ComposerControls
                scope={selectedWorkspaceId ? { kind: "workspace", id: selectedWorkspaceId } : undefined}
                onModelReadyChange={setModelReady}
              />
            </div>
            <div className="flex items-center gap-1">
              <NewConversationAttachmentPicker
                workspaceRoot={selectedWorkspace?.root_path}
                existingPaths={existingPaths}
                addImage={addImage}
                disabled={submitting}
              />
              {blockedReason && (
                <span id={blockedReasonId} className="text-muted-foreground px-1 text-xs">{blockedReason}</span>
              )}
              {/* 没有草稿时只留图标，避免禁用态的文字按钮抢占视线；可访问名称始终是「开始对话」。 */}
              <Button
                type="submit"
                size={hasDraft ? "default" : "icon"}
                className="shrink-0 rounded-full"
                disabled={!canStart}
                aria-label="开始对话"
                title="开始对话"
                aria-describedby={blockedReason ? blockedReasonId : undefined}
              >
              {submitting ? <Loader2Icon className="animate-spin" /> : <SendIcon />}{hasDraft && "开始对话"}
              </Button>
            </div>
          </div>
          <ComposerFeedback />
          </ComposerFeedbackProvider>
        </ComposerSurface>
      </form>
      </ComposerInsertionProvider>
    </div>
  );
}
