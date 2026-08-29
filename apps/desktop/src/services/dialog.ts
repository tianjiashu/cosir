/**
 * 目录选择服务。
 *
 * 封装 Tauri dialog 插件的系统目录选择器调用，
 * 将基础设施层（Tauri/IPC）与组件解耦，组件只调用本服务而非直连插件。
 *
 * @module services/dialog
 */

import { open } from "@tauri-apps/plugin-dialog";

/**
 * 弹出系统目录选择器。
 *
 * @returns 用户选中的目录绝对路径；用户取消选择时返回 null。
 *
 * @throws 不主动捕获异常；选择器调用失败会向上抛出，由调用方决定如何回显。
 */
export async function selectDirectory(): Promise<string | null> {
  const selected = await open({
    directory: true,
    multiple: false,
  });
  return typeof selected === "string" ? selected : null;
}

/**
 * 弹出系统附件选择器。
 *
 * @returns 用户选中的本地绝对路径列表；用户取消选择时返回空数组。
 *
 * @throws 不主动捕获异常；选择器调用失败会向上抛出，由调用方决定如何回显。
 */
export async function selectAttachmentPaths(): Promise<string[]> {
  const selected = await open({
    directory: false,
    multiple: true,
  });
  if (typeof selected === "string") {
    return [selected];
  }
  if (Array.isArray(selected)) {
    return selected.filter((item): item is string => typeof item === "string");
  }
  return [];
}
