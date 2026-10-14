"use client";

import { createContext, useCallback, useContext, useMemo, useState, type PropsWithChildren } from "react";
import { XIcon } from "lucide-react";

type ComposerFeedbackValue = {
  message: string | null;
  show: (message: string) => void;
  clear: () => void;
};

const ComposerFeedbackContext = createContext<ComposerFeedbackValue | null>(null);

export function ComposerFeedbackProvider({ children }: PropsWithChildren) {
  const [message, setMessage] = useState<string | null>(null);
  const show = useCallback((nextMessage: string) => setMessage(nextMessage), []);
  const clear = useCallback(() => setMessage(null), []);
  const value = useMemo(() => ({ message, show, clear }), [clear, message, show]);

  return <ComposerFeedbackContext.Provider value={value}>{children}</ComposerFeedbackContext.Provider>;
}

export function useComposerFeedback() {
  const context = useContext(ComposerFeedbackContext);
  if (!context) throw new Error("ComposerFeedbackProvider is required for composer feedback");
  return context;
}

export function ComposerFeedback() {
  const { message, clear } = useComposerFeedback();
  if (!message) return null;

  return (
    <div role="alert" className="text-destructive flex min-w-0 items-center gap-2 px-2 text-xs">
      <span className="min-w-0 flex-1 break-words">{message}</span>
      <button
        type="button"
        className="focus-visible:ring-ring/50 inline-flex size-6 shrink-0 items-center justify-center rounded-full outline-none focus-visible:ring-2"
        aria-label="关闭附件提示"
        onClick={clear}
      >
        <XIcon className="size-3.5" aria-hidden="true" />
      </button>
    </div>
  );
}
