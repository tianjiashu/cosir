"use client";

import {
  AssistantRuntimeProvider,
  AuiConfig,
  Tools,
  useAui,
  useAssistantTransportRuntime,
} from "@assistant-ui/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Thread } from "@/components/assistant-ui/elements/thread.aui";
import { getApiBaseUrl } from "@/lib/api/client";
import { readStoredSelection } from "@/lib/model-selection-storage";
import {
  extractUserAddMessageText,
  toTransportThreadView,
} from "@/lib/assistant/converter";
import type { TransportState } from "@/lib/assistant/contract";
import { codingAgentToolkit } from "./toolkit";
import { newTraceId, setActiveTraceId } from "@/lib/trace";
import { frontendLog } from "@/lib/logging/frontend-log";

/**
 * 把服务端首屏历史 state 挂载到 assistant-ui runtime 的瘦组件。
 *
 * 该组件在 `initialState` 已就绪后才被外层渲染，且外层用 `key={taskId}` 保证
 * 切换 task 时 runtime 整体重建，避免上一个 task 的历史残留。
 *
 * @param taskId - 当前任务 id，用于拼接后端 assistant 端点路径与模型选择读取。
 * @param initialState - 已就绪的服务端首屏历史 state，作为 runtime 初始 state。
 * @returns 挂载 `AssistantRuntimeProvider` 的对话界面（含 Thread 渲染）。
 * @sideEffects 建立与后端 `/assistant` 的 assistant-transport 连接；
 *   首次挂载只负责历史 state 与后续 fresh commands；正常新消息不走 resume；
 *   模型选择通过 `prepareSendCommandsRequest` 注入发送请求（剥离回传 state，有值才覆盖）。
 */
export function AssistantRuntime({
  taskId,
  initialState,
}: {
  taskId: number;
  initialState: TransportState;
}) {
  // assistant-ui 的 AddMessageCommand 没有幂等 ID；后端需要 commandId 才能安全
  // 重试。用命令对象做弱引用键，保证同一 queued command 在重试/重连时复用 ID，
  // 而不同消息仍获得不同 ID。
  const commandIds = useRef(new WeakMap<object, string>());
  // 一个 runtime 对应一个用户可追踪的 Assistant Transport 操作链路；恢复/重连也复用它。
  const [traceId] = useState(() => newTraceId());
  useEffect(() => {
    setActiveTraceId(traceId);
    return () => setActiveTraceId(null);
  }, [traceId]);
  // 发送失败时把 inTransit 消息文本回填到 composer 的桥接函数；由下方
  // ComposerRestoreBridge 在挂载后注册（runtime options 闭包无法使用 hooks）。
  const composerRestoreRef = useRef<((text: string) => void) | null>(null);
  const registerComposerRestore = useCallback(
    (restore: (text: string) => void) => {
      composerRestoreRef.current = restore;
    },
    [],
  );
  // 发送失败处理所需的 runtime 回调参数形状（刻意取最小字段子集，理由同
  // toTransportThreadView 的第二参数设计）。
  type SendErrorParams = {
    commands: readonly unknown[];
    updateState: (updater: (state: TransportState) => TransportState) => void;
  };
  /**
   * 发送失败处理：把用户文本回填 composer，并把错误写入本地 state 使其可见。
   *
   * assistant-ui 在 HTTP 错误/流错误时会静默重置命令队列，若不接管，用户消息
   * 直接消失且无任何提示。本处理取 inTransit 队列中最后一条非空用户消息文本
   * 回填 composer（消息不丢，用户可直接重发），再经 updateState 把稳定错误
   * 写入本地 `TransportState.error` —— converter（`toTransportThreadView`）
   * 会把它渲染为消息流末尾的错误条；下一次服务端 state 帧到达时自动覆盖消失。
   *
   * @param error - transport 抛出的原始错误（网络异常、HTTP 状态错误等）。
   * @param commands - 失败时仍在传输中的命令队列（inTransit）。
   * @param updateState - runtime 提供的本地 state 更新入口。
   * @returns 无。
   * @sideEffects 调用 ComposerRestoreBridge 注册的 composer.setText；
   *   触发一次本地 state 重渲染。
   */
  const handleSendError = useCallback(async (error: unknown, { commands, updateState }: SendErrorParams) => {
    await frontendLog("ERROR", "assistant_transport_error", "Assistant Transport 请求失败", {
      traceId,
      error,
    });
    const failedText = [...commands]
      .reverse()
      .map((command) => extractUserAddMessageText(command))
      .find((text) => text.trim().length > 0);
    if (failedText) composerRestoreRef.current?.(failedText);
    const message =
      error instanceof Error && error.message
        ? error.message
        : "网络异常，请检查后端服务是否在运行";
    updateState((current) => ({
      ...current,
      error: { code: "SEND_FAILED", message, retryable: true },
    }));
  }, [traceId]);
  const runtime = useAssistantTransportRuntime<TransportState>({
    // 只在 runtime 首次创建时捕获一次，故必须在挂载前把服务端历史灌入。
    initialState,
    protocol: "assistant-transport",
    api: `${getApiBaseUrl()}/assistant`,
    headers: async () => ({
      "Content-Type": "application/json",
      "X-Trace-Id": traceId,
    }),
    prepareSendCommandsRequest: (body) => {
      const selection = readStoredSelection(taskId);
      // 剥离 assistant-ui runtime 硬编码回传的 state 字段：后端已从服务端 turns 事实重建历史，
      // 客户端不持有、不伪造、不回传 state；只保留模型选择注入（T1：有值才覆盖）。
      const commands = body.commands.map((command) => {
        if (command.type !== "add-message") return command;
        const key = command as object;
        let commandId = commandIds.current.get(key);
        if (!commandId) {
          commandId = crypto.randomUUID();
        }
        commandIds.current.set(key, commandId);
        return { ...command, commandId };
      });
      const rest = Object.fromEntries(
        Object.entries(body).filter(([key]) => key !== "state"),
      );
      rest.commands = commands;
      const request = {
        ...rest,
        taskId,
        // Task 是领域唯一身份；Transport threadId 统一采用 task-{taskId}。
        threadId: `task-${taskId}`,
      };
      void frontendLog("INFO", "assistant_transport_request_prepared", "Assistant Transport 请求已组装", {
        traceId,
        data: {
          taskId,
          commandCount: commands.length,
          commandTypes: commands.map((command) => command.type),
          isResume: commands.length === 0,
        },
      });
      if (!selection) return request;
      return {
        ...request,
        ...(selection.providerId !== undefined && { providerId: selection.providerId }),
        ...(selection.modelName !== undefined && { modelName: selection.modelName }),
        ...(selection.reasoningEffort !== undefined && { reasoningEffort: selection.reasoningEffort }),
      };
    },
    onResponse: async (response) => {
      await frontendLog("INFO", "assistant_transport_response", "Assistant Transport 已收到响应", {
        traceId,
        data: { taskId, status: response.status, ok: response.ok },
      });
    },
    onError: handleSendError,
    // 纯映射函数：仅把后端 state 快照与待发送命令翻译为 assistant-ui 数据格式，
    // 不写 React state、不产生副作用（停止按钮所需的 runId 由它自己通过
    // 官方 useAssistantTransportState 读取，不在此处暴露）。
    converter: (state, connectionMetadata) =>
      toTransportThreadView(state, connectionMetadata),
  });
  const config = AuiConfig({ tools: Tools({ toolkit: codingAgentToolkit }) });

  return (
    <AssistantRuntimeProvider runtime={runtime} config={config}>
      <ComposerRestoreBridge register={registerComposerRestore} />
      <div className="h-dvh">
        <Thread taskId={taskId} />
      </div>
    </AssistantRuntimeProvider>
  );
}

/**
 * 把 composer 回填函数注册给 runtime options 闭包的副作用组件。
 *
 * `useAssistantTransportRuntime` 的 onError 闭包创建于组件渲染期，无法在其中
 * 使用 hooks 获取 composer；本组件在 Provider 内部挂载后，用 `useAui` 组装
 * `composer.setText` 回填函数并注册到调用方 ref，供发送失败时恢复用户输入。
 * 组件不渲染任何可见 UI。
 *
 * @param register - 回填函数注册器（稳定引用，写入调用方持有的 ref）。
 * @returns 恒为 null（纯副作用组件）。
 * @sideEffects 挂载后（及 aui 实例变化时）重新注册回填函数；组件卸载不注销
 *   （ref 由调用方生命周期管理，随 AssistantRuntime 一起销毁）。
 */
function ComposerRestoreBridge({
  register,
}: {
  register: (restore: (text: string) => void) => void;
}) {
  const aui = useAui();
  useEffect(() => {
    register((text) => {
      aui.composer.setText(text);
    });
  }, [aui, register]);
  return null;
}
