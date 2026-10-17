"use client";

import { createContext, useContext, type PropsWithChildren } from "react";

export type ComposerEditorViewState = {
  disabled: boolean;
  hydrating: boolean;
};

const ComposerEditorViewContext = createContext<ComposerEditorViewState | null>(null);

export function ComposerEditorViewProvider({
  children,
  value,
}: PropsWithChildren<{ value: ComposerEditorViewState }>) {
  return <ComposerEditorViewContext.Provider value={value}>{children}</ComposerEditorViewContext.Provider>;
}

export function useComposerEditorView(): ComposerEditorViewState {
  const context = useContext(ComposerEditorViewContext);
  if (!context) throw new Error("ComposerEditorViewProvider is required for composer token nodes");
  return context;
}
