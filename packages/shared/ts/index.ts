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
export * from "./api";
export * from "./backend";
export * from "./runs";
export * from "./approvals";
export * from "./toolExecution";
