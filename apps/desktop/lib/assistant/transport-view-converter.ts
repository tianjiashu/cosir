import type { ThreadAssistantMessage, ThreadMessage } from "@assistant-ui/react";
import type { ReadonlyJSONValue } from "assistant-stream/utils";

import type {
  TransportError,
  TransportMessage,
  TransportRun,
  TransportState,
} from "@/lib/assistant/contract";
import {
  isUserAddMessageCommand,
  toPendingUserMessage,
  toSnapshotErrorMessage,
  toThreadMessageWithRenderContext,
  type UserAddMessageCommand,
  type TransportMessageRenderContext,
} from "@/lib/assistant/converter";

export type TransportMessageInput = TransportMessageRenderContext & {
  source: TransportMessage;
};

export type TransportViewConnectionMetadata = {
  pendingCommands: readonly unknown[];
  isSending: boolean;
  /** Assistant UI metadata is intentionally not a canonical message input. */
  toolStatuses?: Record<string, unknown>;
};

export type TransportThreadView = {
  messages: ThreadMessage[];
  isRunning: boolean;
  state: ReadonlyJSONValue;
};

export type TransportViewConverter = (
  state: TransportState,
  connectionMetadata: TransportViewConnectionMetadata,
) => TransportThreadView;

export type TransportViewConverterDiagnostics = {
  /** Test-only hook; receives identifiers, never message content. */
  onCanonicalMessageConverted?: (messageId: string) => void;
  /** Test-only hook for a newly projected pending command. */
  onPendingMessageConverted?: () => void;
  /** Test-only hook for a newly projected snapshot error. */
  onSnapshotErrorConverted?: () => void;
};

type CachedMessage = {
  signature: string;
  value: ThreadMessage;
};

type CachedPendingMessage = {
  signature: string;
  value: ThreadMessage | null;
};

type CachedSnapshotError = {
  error: TransportError;
  signature: string;
  value: ThreadAssistantMessage;
};

const SIGNATURE_SEPARATOR = "\u001f";

function signaturePart(value: string | null | undefined): string {
  if (value === null || value === undefined) return "-";
  return `${value.length}:${value}`;
}

function messageRenderSignature(context: TransportMessageRenderContext): string {
  return [
    signaturePart(String(context.runId)),
    signaturePart(context.runStatus),
    signaturePart(context.endReason),
    // 受控错误参与签名：同一 failed 状态下 message 变化（例如重连后重新分类）必须让缓存
    // 失效，否则界面会一直显示旧的失败提示。
    signaturePart(
      context.error === null ? null : `${context.error.code}|${context.error.message}`,
    ),
    context.isLastRunMessage ? "last" : "not-last",
  ].join(SIGNATURE_SEPARATOR);
}

function pendingCommandSignature(command: UserAddMessageCommand): string {
  const partsSignature = command.message.parts
    .map((part) => [
      signaturePart(part.type),
      signaturePart(
        part.type === "text" ? part.text
          : part.type === "image" ? part.image
          : part.type === "file" ? `${part.data}:${part.filename ?? ""}:${part.mimeType}`
              : "",
      ),
    ].join(SIGNATURE_SEPARATOR))
    .join(SIGNATURE_SEPARATOR);
  return [
    signaturePart(command.type),
    signaturePart(command.sourceId),
    signaturePart(command.parentId),
    signaturePart(command.message.role),
    signaturePart(partsSignature),
  ].join(SIGNATURE_SEPARATOR);
}

function snapshotErrorSignature(error: TransportError): string {
  return [signaturePart(error.code), signaturePart(error.message)].join(SIGNATURE_SEPARATOR);
}

function lastAssistantMessageId(run: TransportRun): string | null {
  return [...run.messages].reverse().find((message) => message.role === "assistant")?.id ?? null;
}

/**
 * Creates the task/runtime-scoped converter used by Assistant Transport.
 *
 * The returned ordinary converter owns WeakMap caches for canonical messages
 * and pending commands. It intentionally does not call assistant-ui React
 * hooks: the current Transport API invokes a plain converter callback, while
 * assistant-ui's cached `useThreadMessages` path is a separate hook boundary.
 * The cache is process-local WebView state only; it is discarded when the
 * runtime session is discarded and never becomes conversation state.
 */
export function createTransportViewConverter(
  diagnostics: TransportViewConverterDiagnostics = {},
): TransportViewConverter {
  const messageCache = new WeakMap<TransportMessage, CachedMessage>();
  const pendingMessageCache = new WeakMap<object, CachedPendingMessage>();
  let snapshotErrorCache: CachedSnapshotError | null = null;

  const convertMessage = (input: TransportMessageInput): ThreadMessage => {
    const signature = messageRenderSignature(input);
    const cached = messageCache.get(input.source);
    if (cached?.signature === signature) return cached.value;

    const value = toThreadMessageWithRenderContext(input.source, input);
    messageCache.set(input.source, { signature, value });
    diagnostics.onCanonicalMessageConverted?.(input.source.id);
    return value;
  };

  const convertPendingCommand = (command: unknown): ThreadMessage | null => {
    if (!isUserAddMessageCommand(command)) return null;

    const cached = pendingMessageCache.get(command);
    const signature = pendingCommandSignature(command);
    if (cached?.signature === signature) return cached.value;

    const value = toPendingUserMessage(command);
    pendingMessageCache.set(command, { signature, value });
    diagnostics.onPendingMessageConverted?.();
    return value;
  };

  const convertSnapshotError = (error: TransportError | null): ThreadAssistantMessage | null => {
    if (error === null) {
      snapshotErrorCache = null;
      return null;
    }

    const signature = snapshotErrorSignature(error);
    if (snapshotErrorCache?.error === error && snapshotErrorCache.signature === signature) {
      return snapshotErrorCache.value;
    }

    const value = toSnapshotErrorMessage(error);
    snapshotErrorCache = { error, signature, value };
    diagnostics.onSnapshotErrorConverted?.();
    return value;
  };

  return (state, connectionMetadata) => {
    const messages = state.runs.flatMap((run) => {
      const lastMessageId = lastAssistantMessageId(run);
      return run.messages.map((source) => convertMessage({
        source,
        runId: run.runId,
        runStatus: run.status,
        endReason: run.endReason,
        error: run.error,
        isLastRunMessage: source.id === lastMessageId,
      }));
    });

    const snapshotError = convertSnapshotError(state.error);
    if (snapshotError !== null) messages.push(snapshotError);

    const pendingMessages = connectionMetadata.pendingCommands
      .map(convertPendingCommand)
      .filter((message): message is ThreadMessage => message !== null);
    const currentRun = state.current_run_id === null
      ? null
      : state.runs.find((run) => run.runId === state.current_run_id) ?? null;
    const hasPendingCommands = connectionMetadata.pendingCommands.length > 0;
    const currentRunIsTerminal = currentRun !== null
      && ["idle", "completed", "failed", "cancelled", "interrupted"].includes(currentRun.status);

    return {
      messages: [...messages, ...pendingMessages],
      // Server Run terminal state remains authoritative over transport sending.
      isRunning: hasPendingCommands
        || (currentRun !== null ? !currentRunIsTerminal : connectionMetadata.isSending),
      state: state as unknown as ReadonlyJSONValue,
    };
  };
}
