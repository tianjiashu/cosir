import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  type ReactNode,
} from "react";
import { toast } from "sonner";
import type { Capsule } from "@/lib/assistant/capsules";
import { getDirectory, getFileInfo } from "@/lib/api/workspace-files";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { refIsDirectory, refWorkspacePath } from "@/lib/workspace-references/ref-capsule";

/** 输入框一侧收到的胶囊：在光标处插入这些标签，可再附一句预设提问。 */
type Receiver = (capsules: Capsule[], question?: string) => void;
type Bridge = {
  target: string | null;
  workspaceId: number | null;
  workspaceRoot: string;
  workspaceName: string;
  register: (target: string, receiver: Receiver) => () => void;
  /** 「添加到对话」入口能否点：有可见的对话输入框且属于这个工作区。各入口只用它决定禁用态。 */
  canAdd: (workspace: number) => boolean;
  /** 迟到回调用：读最新工作区，不随发起组件的渲染闭包过期。 */
  isCurrentWorkspace: (workspace: number) => boolean;
  /**
   * 唯一的添加边界：再次确认目标输入框仍在（可能刚切走），不在就提示；工作区引用逐条确认目标仍然
   * 存在（文件读一次文件信息、目录读一次目录页），不存在的引用逐条提示且不插入。本机文件与 Skill
   * 由后端在发送时校验，这里不查。
   */
  add: (workspace: number, capsules: Capsule[], question?: string) => Promise<void>;
};
const Context = createContext<Bridge | null>(null);
export const ReferenceTargetScope = createContext<string | null>(null);
export const useReferenceBridge = () => useContext(Context);

/** 只连接当前可见 Composer，避免把文件引用插入后台保活的 Task。 */
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
  const register = useCallback((key: string, callback: Receiver) => {
    const entry = { target: key, callback };
    receivers.current.push(entry);
    return () => {
      receivers.current = receivers.current.filter((item) => item !== entry);
    };
  }, []);
  const add = useCallback(
    async (workspace: number, capsules: Capsule[], question?: string) => {
      const destination = receivers.current.at(-1);
      if (
        !destination ||
        destination.target !== current.current.target ||
        workspace !== current.current.workspaceId
      ) {
        toast.error("请先打开当前工作区的对话输入框");
        return;
      }
      const accepted: Capsule[] = [];
      const failed: string[] = [];
      for (const capsule of capsules) {
        try {
          await confirmWorkspaceTarget(workspace, capsule);
          accepted.push(capsule);
        } catch (error) {
          failed.push(
            `${capsule.kind === "ref" ? capsule.path || "工作区" : capsule.kind}：${userErrorMessage(error, "添加失败")}`,
          );
          void frontendLog(
            "WARNING",
            "workspace_reference_missing",
            "工作区引用不存在",
            { error, data: { workspaceId: workspace, capsule } },
          );
        }
      }
      if (accepted.length) destination.callback(accepted, question);
      if (failed.length) toast.error(failed.join("\n"), { duration: 8000 });
    },
    [],
  );
  const isCurrentWorkspace = useCallback((workspace: number) => workspace === current.current.workspaceId, []);
  const canAdd = useCallback(
    (workspace: number) => target !== null && workspace === workspaceId,
    [target, workspaceId],
  );
  const value = useMemo(
    () => ({
      target,
      workspaceId,
      workspaceRoot,
      workspaceName,
      register,
      canAdd,
      isCurrentWorkspace,
      add,
    }),
    [target, workspaceId, workspaceRoot, workspaceName, register, canAdd, isCurrentWorkspace, add],
  );
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

/**
 * 工作区引用在插入前确认目标存在（目录读目录页、文件读文件信息），读取失败（不存在、无权限）即不插入。
 * 本机文件与 Skill 不经过工作区文件接口，交给后端发送时校验。
 */
async function confirmWorkspaceTarget(workspace: number, capsule: Capsule): Promise<void> {
  if (capsule.kind !== "ref") return;
  const path = refWorkspacePath(capsule);
  if (refIsDirectory(capsule)) await getDirectory(workspace, path, undefined);
  else await getFileInfo(workspace, path);
}
