import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { toast } from "sonner";
import {
  prepareReference,
  type ReferenceInput,
} from "@/lib/workspace-references/prepare";
import type { WorkspaceReference } from "@/lib/workspace-references/codec";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";

export type ReferenceReplacement = { from: number; to: number; text: string };
type Receiver = (
  references: WorkspaceReference[],
  question?: string,
  replacement?: ReferenceReplacement,
) => void;
type Bridge = {
  target: string | null;
  workspaceId: number | null;
  workspaceRoot: string;
  workspaceName: string;
  busy: boolean;
  register: (target: string, receiver: Receiver) => () => void;
  add: (
    workspace: number,
    inputs: ReferenceInput[],
    question?: string,
    replacement?: ReferenceReplacement,
  ) => Promise<void>;
  cancel: () => void;
};
const Context = createContext<Bridge | null>(null);
export const ReferenceTargetScope = createContext<string | null>(null);
export const useReferenceBridge = () => useContext(Context);

/** 只连接当前可见 Composer；异步准备完成时再次核对目标，不向后台保活的 Task 插入。 */
export function ReferenceBridgeProvider({
  target,
  workspaceId,
  workspaceRoot,
  workspaceName,
  children,
}: {
  target: string | null;
  workspaceId: number | null;
  workspaceRoot: string;
  workspaceName: string;
  children: ReactNode;
}) {
  const current = useRef({ target, workspaceId });
  current.current = { target, workspaceId };
  const receivers = useRef<{ target: string; callback: Receiver }[]>([]);
  const pending = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    pending.current?.abort();
    setBusy(false);
    return () => pending.current?.abort();
  }, [target, workspaceId]);
  const register = useCallback((key: string, callback: Receiver) => {
    const entry = { target: key, callback };
    receivers.current.push(entry);
    return () => {
      receivers.current = receivers.current.filter((item) => item !== entry);
    };
  }, []);
  const cancel = useCallback(() => {
    pending.current?.abort();
    setBusy(false);
  }, []);
  const add = useCallback(
    async (
      workspace: number,
      inputs: ReferenceInput[],
      question?: string,
      replacement?: ReferenceReplacement,
    ) => {
      const destination = receivers.current.at(-1);
      if (
        !destination ||
        destination.target !== current.current.target ||
        workspace !== current.current.workspaceId
      ) {
        toast.error("请先打开当前工作区的对话输入框");
        return;
      }
      pending.current?.abort();
      const controller = new AbortController();
      pending.current = controller;
      setBusy(true);
      const references: WorkspaceReference[] = [];
      const failed: string[] = [];
      try {
        for (const input of inputs) {
          if (controller.signal.aborted) return;
          try {
            const ref = await prepareReference(
              workspace,
              input,
              controller.signal,
            );
            references.push(ref);
          } catch (error) {
            if (controller.signal.aborted) return;
            failed.push(
              `${input.path || "工作区"}：${userErrorMessage(error, "添加失败")}`,
            );
            void frontendLog(
              "WARNING",
              "workspace_reference_prepare_failed",
              "准备引用失败",
              { error, data: { workspaceId: workspace, path: input.path } },
            );
          }
        }
        if (
          controller.signal.aborted ||
          receivers.current.at(-1) !== destination ||
          current.current.target !== destination.target ||
          current.current.workspaceId !== workspace
        )
          return;
        if (references.length)
          destination.callback(references, question, replacement);
        if (failed.length) toast.error(failed.join("\n"), { duration: 8000 });
      } finally {
        if (pending.current === controller) setBusy(false);
      }
    },
    [],
  );
  const value = useMemo(
    () => ({
      target,
      workspaceId,
      workspaceRoot,
      workspaceName,
      busy,
      register,
      add,
      cancel,
    }),
    [
      target,
      workspaceId,
      workspaceRoot,
      workspaceName,
      busy,
      register,
      add,
      cancel,
    ],
  );
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
