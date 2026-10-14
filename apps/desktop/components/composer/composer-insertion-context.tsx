"use client";

import { createContext, useCallback, useContext, useMemo, useRef, type PropsWithChildren } from "react";
import type { InlineComposerAttachment, InlineComposerSkill, ComposerInsertionCommand } from "@/components/composer/composer-document";

type ComposerInsertionContextValue = {
  register: (handler: (command: ComposerInsertionCommand) => void) => () => void;
  dispatch: (command: ComposerInsertionCommand) => void;
};

const ComposerInsertionContext = createContext<ComposerInsertionContextValue | null>(null);

/** 为一个 composer 子树提供唯一的编辑器插入命令入口。 */
export function ComposerInsertionProvider({ children }: PropsWithChildren) {
  const handlerRef = useRef<((command: ComposerInsertionCommand) => void) | null>(null);
  const register = useCallback((handler: (command: ComposerInsertionCommand) => void) => {
    handlerRef.current = handler;
    return () => {
      if (handlerRef.current === handler) handlerRef.current = null;
    };
  }, []);
  const dispatch = useCallback((command: ComposerInsertionCommand) => {
    handlerRef.current?.(command);
  }, []);
  const value = useMemo(() => ({ register, dispatch }), [dispatch, register]);

  return <ComposerInsertionContext.Provider value={value}>{children}</ComposerInsertionContext.Provider>;
}

/** 读取当前 composer 的命令入口；没有 Provider 表示控件装配错误。 */
export function useComposerInsertion() {
  const context = useContext(ComposerInsertionContext);
  if (!context) throw new Error("ComposerInsertionProvider is required for composer insertion controls");
  return context;
}

export function insertAttachments(
  dispatch: ComposerInsertionContextValue["dispatch"],
  attachments: readonly InlineComposerAttachment[],
) {
  dispatch({ type: "insert-attachments", attachments });
}

export function insertText(dispatch: ComposerInsertionContextValue["dispatch"], text: string) {
  dispatch({ type: "insert-text", text });
}

export function insertSkill(dispatch: ComposerInsertionContextValue["dispatch"], skill: InlineComposerSkill) {
  dispatch({ type: "insert-skill", skill });
}
