/**
 * 工作区共享类型定义。
 *
 * 与后端 `WorkspaceRecord.to_dict()` 输出保持一致，
 * 作为前后端共享的 workspace 契约事实源。
 *
 * @module shared/workspace
 */

/** 工作区记录接口，对应后端 `WorkspaceRecord.to_dict()` 输出。 */
export interface WorkspaceRecord {
  /** 唯一的工作区标识符。 */
  workspace_id: string;
  /** 用户可读的工作区名称。 */
  name: string;
  /** 工作区本地文件系统路径。 */
  root_path: string;
  /** 工作区创建时间戳。 */
  created_at: string;
  /** 工作区最近更新时间戳。 */
  updated_at: string;
}
