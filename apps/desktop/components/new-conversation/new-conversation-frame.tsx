import type { ReactNode } from "react";

import { NewConversationBackdrop } from "@/components/new-conversation/backdrop";
import { NewConversationKeyHints } from "@/components/new-conversation/key-hints";
import { WorkspacePicker, type WorkspacePickerProps } from "@/components/new-conversation/workspace-picker";

/**
 * 新会话页的版面：点阵背景、标题区（工作区选择器 + 「想做什么？」）、输入框、键位提示，上下留白 2:3。
 *
 * 版面只在这里，中间那一格由调用方放进来：新会话页就是未开始的会话，放的是它的输入框（经
 * `EmptyThreadFrameContext` 交给线程，加载时是同形状的骨架）；`/` 还没解析到会话时放占位或提示（`NewConversation`）。
 */
export function NewConversationFrame({ children, ...picker }: WorkspacePickerProps & { children: ReactNode }) {
  return (
    // 背景包在滚动容器外，内容滚动时点阵不跟着走。
    <NewConversationBackdrop>
      {/* 上下留白 2:3，把视觉中心放在略高于正中处；内容超高时整页滚动。 */}
      <div className="mx-auto grid h-full w-full max-w-conversation grid-rows-[2fr_auto_3fr] overflow-y-auto px-6">
        {/* 新会话页的输入区起步更高。 */}
        <div className="row-start-2 w-full [--composer-min-height:5rem]">
          {/* 命令行提示符式标题：目录段就是工作区选择器，当前工作区不在输入框里重复显示。 */}
          <h1 className="mb-6 flex min-w-0 items-center justify-center gap-2 text-2xl font-medium">
            <WorkspacePicker {...picker} />
            <span aria-hidden="true" className="text-brand font-mono">❯</span>
            <span className="shrink-0">想做什么？</span>
          </h1>
          {children}
          <NewConversationKeyHints />
        </div>
      </div>
    </NewConversationBackdrop>
  );
}
