import { createContext, useCallback, useContext, useMemo, useRef, type PropsWithChildren } from "react";
import type { Capsule } from "@/lib/assistant/capsules";
import type { ComposerInsertionCommand } from "@/components/composer/composer-document";

type ComposerInsertionContextValue = {
  register: (handler: (command: ComposerInsertionCommand) => void, isEditable: () => boolean) => () => void;
  isAvailable: () => boolean;
  dispatch: (command: ComposerInsertionCommand) => void;
};

const ComposerInsertionContext = createContext<ComposerInsertionContextValue | null>(null);

/** 为一个 composer 子树提供唯一的编辑器插入命令入口。 */
export function ComposerInsertionProvider({ children }: PropsWithChildren) {
  const handlerRef = useRef<{ insert: (command: ComposerInsertionCommand) => void; isEditable: () => boolean } | null>(null);
  const register = useCallback((insert: (command: ComposerInsertionCommand) => void, isEditable: () => boolean) => {
    const handler = { insert, isEditable };
    handlerRef.current = handler;
    return () => {
      if (handlerRef.current === handler) handlerRef.current = null;
    };
  }, []);
  const isAvailable = useCallback(() => handlerRef.current?.isEditable() ?? false, []);
  const dispatch = useCallback((command: ComposerInsertionCommand) => {
    if (handlerRef.current?.isEditable()) handlerRef.current.insert(command);
  }, []);
  const value = useMemo(() => ({ register, dispatch, isAvailable }), [dispatch, register, isAvailable]);

  return <ComposerInsertionContext.Provider value={value}>{children}</ComposerInsertionContext.Provider>;
}

/** 读取当前 composer 的命令入口；没有 Provider 表示控件装配错误。 */
export function useComposerInsertion() {
  const context = useContext(ComposerInsertionContext);
  if (!context) throw new Error("ComposerInsertionProvider is required for composer insertion controls");
  return context;
}

export function insertCapsules(
  dispatch: ComposerInsertionContextValue["dispatch"],
  capsules: readonly Capsule[],
) {
  if (capsules.length === 0) return;
  dispatch({ type: "insert-capsules", capsules });
}

export function insertText(dispatch: ComposerInsertionContextValue["dispatch"], text: string) {
  dispatch({ type: "insert-text", text });
}
