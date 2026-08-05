/**
 * 共享类型统一导出入口。
 *
 * 前端与后端解耦引用此文件，避免在各处重复定义类型。
 * 所有类型定义均以对应模块为单一事实源。
 *
 * @module shared
 */

export * from "./events";
export * from "./task";
export * from "./turn";
export * from "./workspace";
export * from "./workspaceEvent";
export * from "./agents";
export {
  API_BASE,
  API_PATHS,
  type BackendHealthResponse,
  type CreateTaskRequest,
  type CreateTurnRequest,
  type CreateWorkspaceRequest,
  type DeleteWorkspaceResponse,
  type ListAgentsResponse,
} from "./api";
export * from "./backend";
export * from "./toolDisplay";
export * from "./toolDisplayRules";
export * from "./toolExecution";
export * from "./tracePropagation";
export * from "./logs";
