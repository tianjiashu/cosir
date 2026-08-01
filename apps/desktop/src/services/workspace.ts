/**
 * 工作区创建服务。
 *
 * 封装「弹出系统目录选择器 -> 注册工作区 -> 设为活跃」这一复用流程，
 * 供新建任务页使用，避免逻辑重复。
 *
 * @module services/workspace
 */

import { selectDirectory } from "@/services/dialog";
import * as api from "@/services/api";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { basenameOf } from "@/lib/utils";
import type { WorkspaceRecord } from "@shared/workspace";

/**
 * 弹出系统目录选择器，将所选目录注册为工作区并设为活跃。
 *
 * 选择后直接调用后端创建工作区，工作区名称取目录末级名；
 * 取消选择不做任何处理；选择失败向上抛出，由调用方决定如何回显。
 *
 * @returns 创建成功的工作区记录；用户取消选择时返回 null。
 *
 * @throws 当目录选择或创建工作区失败时抛出原始错误，由调用方回显。
 *
 * @sideeffect 写入 workspaceStore：新增工作区并将其设为 activeWorkspaceId。
 */
export async function pickAndCreateWorkspace(): Promise<WorkspaceRecord | null> {
  const selected = await selectDirectory();
  if (!selected) {
    return null;
  }
  const name = basenameOf(selected);
  const workspace = await api.createWorkspace({ name, root_path: selected });
  const { upsertWorkspace, setActiveWorkspace } = useWorkspaceStore.getState();
  upsertWorkspace(workspace);
  setActiveWorkspace(workspace.workspace_id);
  return workspace;
}
