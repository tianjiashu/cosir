import { UserFacingError } from "@/lib/errors/user-facing-error";
import {
  useExternalStoreRuntime,
  type AppendMessage,
  type ExternalStoreAdapter,
} from "@assistant-ui/react";
import type { ReadonlyJSONValue } from "assistant-stream/utils";
import { useCallback, useMemo, useRef, useSyncExternalStore } from "react";

import {
  convertFrameStoreItem,
  type FrameStoreItem,
  type TransportFrameStore,
} from "@/lib/assistant/transport/transport-frame-store";
import type { UserAddMessageCommand } from "@/lib/assistant/transport-message-converter";
import type { SessionTransportConnection } from "@/lib/assistant/transport/session-transport-connection";
import { getBackendRuntimeSnapshot } from "@/lib/backend/runtime-config";

export type SessionAssistantTransportOptions = {
  adapters?: ExternalStoreAdapter<FrameStoreItem>["adapters"];
  /**
   * 提交一条用户消息。宿主负责请求体组装；后端处理完这次命令才返回，被拒时抛错（乐观消息随即
   * 收回），输出经长订阅到达；乐观消息保留到 snapshot 里出现对应消息才清除。只读 runtime 不提供。
   */
  submitMessage?: (command: UserAddMessageCommand) => Promise<void>;
  /** 命令提交被拒或未能发出（HTTP 非 2xx、网络失败、附件失效）；乐观消息已先行收回。 */
  onError?: (error: Error, commands: readonly UserAddMessageCommand[]) => Promise<void> | void;
  readonly?: boolean;
};

/**
 * 把一个已就绪（收到首帧）的 Session 连接交给 assistant-ui：store 快照作为 external store，发送经连接提交。
 *
 * 订阅、重连与 store 的创建都归 `SessionTransportConnection`（外壳持有），这里只做 React 装配，所以 runtime
 * 挂载时一定已有完整状态，渲染出错重挂也不丢数据。不创建业务持久化状态，也不创建额外线程列表（直接用
 * external-store runtime，避免远程 thread-list 的异步绑定阶段）。
 */
export function useSessionAssistantTransportRuntime(
  connection: SessionTransportConnection,
  store: TransportFrameStore,
  options: SessionAssistantTransportOptions = {},
) {
  const view = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  const optionsRef = useRef(options);
  optionsRef.current = options;

  // 气泡里没有编辑框：编辑是撤回后放回主输入框再发送，提交只有新消息这一条路。
  const onNew = useCallback((message: AppendMessage) => {
    const command = toAddMessageCommand(message);
    const current = optionsRef.current;
    // 后端暂停时 UI 仍存活，AUI 已清空提交草稿；在当前 UI 入口恢复并提示，不发送 HTTP。
    // 附件上传后提交也走此入口。
    if (!getBackendRuntimeSnapshot().available) {
      return current.onError?.(new UserFacingError("本机后端暂不可用，请稍后重试"), [command]);
    }
    const send = current.submitMessage;
    if (!send) throw new Error("只读 Transport runtime 不接受消息提交");
    return connection.submit(command, {
      send,
      onError: (error, commands) => optionsRef.current.onError?.(error, commands),
    });
  }, [connection]);
  // 重新拉取即重开长订阅：attach 首帧就是完整 canonical state。
  const onRefetchThread = useCallback(() => connection.open(), [connection]);

  const adapter = useMemo<ExternalStoreAdapter<FrameStoreItem>>(() => {
    const base = {
      messages: view.items,
      isRunning: view.isRunning,
      isDisabled: options.readonly,
      isSendDisabled: options.readonly,
      state: view.state as unknown as ReadonlyJSONValue,
      ...(options.readonly ? {} : { onNew }),
      onRefetchThread,
      convertMessage: convertFrameStoreItem,
      adapters: options.adapters,
    };
    // 库类型把 onNew 建模为 writable runtime 的必填项；readonly runtime 有意在运行时省略，
    // 使 Assistant UI 不暴露写入处理器。
    return base as unknown as ExternalStoreAdapter<FrameStoreItem>;
  }, [onNew, onRefetchThread, options.adapters, options.readonly, view]);

  return { runtime: useExternalStoreRuntime(adapter), loadEarlierHistory: connection.loadEarlierHistory };
}

export function toAddMessageCommand(message: AppendMessage): UserAddMessageCommand {
  const parts: Array<UserAddMessageCommand["message"]["parts"][number]> = [];
  for (const part of message.content) {
    if (part.type === "text") {
      parts.push({ type: "text", text: part.text });
      continue;
    }
    if (part.type === "image") {
      parts.push({ type: "image", image: part.image });
      continue;
    }
    // 普通文件、引用与 Skill 都是正文里的胶囊标签，不会以 file part 出现；
    // 本机附件上传适配器只产出图片。
  }

    // assistant-ui 将上传附件保存在 `message.attachments`，composer 不会把它们复制到
    // `message.content`。因此图片必须在这里从附件内容跨过 Transport 边界，否则输入框
    // 看得到图片，但 add-message command 和 canonical user snapshot 会静默缺少它。
  const seenImages = new Set(
    parts
      .filter((part): part is { type: "image"; image: string } => part.type === "image")
      .map((part) => part.image),
  );
  for (const attachment of message.attachments ?? []) {
    for (const attachmentPart of attachment.content ?? []) {
      if (attachmentPart.type !== "image" || seenImages.has(attachmentPart.image)) continue;
      parts.push({ type: "image", image: attachmentPart.image });
      seenImages.add(attachmentPart.image);
    }
  }

  return {
    type: "add-message",
    message: { role: "user", parts },
  };
}
