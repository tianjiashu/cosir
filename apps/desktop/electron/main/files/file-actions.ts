import fs from "node:fs";
import path from "node:path";
import { dialog, nativeImage, shell, type MessageBoxOptions } from "electron";

import { fileCapabilities } from "../../shared/file-capabilities";
import { logDesktopEvent } from "../desktop-log";
import { HostUserError } from "../host-user-error";
import { showMainWindow, getMainWindow } from "../window";
import { isLaunchable } from "./launchable-file";

/**
 * 宿主对本机文件的系统操作：在文件管理器里显示、用默认应用打开、快速查看、取系统缩略图。只有宿主能调这些系统能力，
 * 所以归 main；入参在 `ipc.ts` 一处校验成存在的绝对路径后才到这里。
 *
 * 路径来自渲染进程（交付物记录、工作区文件信息），宿主不核对来源：渲染进程本就持有后端身份，能让后端写出文件再交付，
 * 核对来源挡不住它。真正要守的是「打开即运行」这个动作，见 `openExternally`。
 */

/** 在系统文件管理器里显示并选中。 */
export function revealFile(target: string): void {
  shell.showItemInFolder(target);
}

const LAUNCH_CHOICES = ["在文件夹中显示", "仍然运行", "取消"] as const;

/**
 * 可运行的文件先由用户在原生对话框里确认：交付物可能是模型写的脚本（被网页或外部内容诱导），用户点「用系统应用打开」
 * 以为是查看，实际是运行。对话框归宿主，渲染进程绕不过；默认选项是在文件夹中显示。返回是否继续打开。
 */
async function confirmLaunch(target: string): Promise<boolean> {
  const options: MessageBoxOptions = {
    type: "warning",
    message: `「${path.basename(target)}」是可运行的程序或脚本`,
    detail: "用系统应用打开会直接运行它。只运行你确认来源可靠的文件。",
    buttons: [...LAUNCH_CHOICES],
    defaultId: 0,
    cancelId: 2,
    noLink: true,
  };
  const window = getMainWindow();
  const { response } = window ? await dialog.showMessageBox(window, options) : await dialog.showMessageBox(options);
  logDesktopEvent("INFO", "file_actions", "launchable_file_confirm", { extension: path.extname(target), choice: LAUNCH_CHOICES[response] ?? "取消" });
  if (response === 0) revealFile(target);
  return response === 1;
}

/** 交给系统按默认应用打开（可运行的文件先确认）；系统打不开（没有关联应用等）时把原因告诉用户。 */
export async function openExternally(target: string): Promise<void> {
  if (isLaunchable(target) && !(await confirmLaunch(target))) return;
  const failure = await shell.openPath(target);
  if (failure) throw new HostUserError(`无法用系统应用打开：${failure}`);
  logDesktopEvent("INFO", "file_actions", "file_opened_externally", { extension: path.extname(target) });
}

/**
 * 系统的快速查看窗口（访达里按空格的那个），只有 macOS 有。两条实测约束（设计 §8 第 4 项）：文件不存在时窗口照样
 * 打开、显示空白——入参校验已确认存在；主窗口隐藏时窗口开在其他应用后面——先显示并聚焦主窗口。
 */
export function quickLook(target: string): void {
  if (!fileCapabilities(process.platform).quickLook) throw new HostUserError("快速查看只在 macOS 上可用");
  showMainWindow();
  getMainWindow()?.previewFile(target);
}

/**
 * 操作系统生成的缩略图（macOS、Windows），返回 PNG 的 data URL；`edge` 是最长边（像素）。系统对不认识的格式不报错，
 * 而是给一张通用图标，所以只对后端判为系统可预览（`FileInfo.system_preview`）的文件调用，由渲染进程负责。
 */
export async function thumbnail(target: string, edge: number): Promise<string> {
  if (!fileCapabilities(process.platform).thumbnail) throw new HostUserError("当前系统不提供缩略图");
  const image = await nativeImage.createThumbnailFromPath(target, { width: edge, height: edge });
  if (image.isEmpty()) throw new HostUserError("系统没有给出这个文件的缩略图");
  return image.toDataURL();
}

/** 入参校验：存在的绝对路径（显示、打开、快速查看对不存在的路径都会「没反应」或开出空白窗口）。 */
export function parseExistingPath(value: unknown): string {
  if (typeof value !== "string" || value.length > 4096 || !path.isAbsolute(value)) throw new HostUserError("文件路径不合法");
  if (!fs.existsSync(value)) throw new HostUserError("文件已不存在");
  return value;
}

/** 缩略图边长：渲染进程只按显示尺寸的两倍要，限制在合理范围内，避免一次生成超大图。 */
export function parseThumbnailEdge(value: unknown): number {
  if (!Number.isSafeInteger(value) || (value as number) < 16 || (value as number) > 1024) throw new HostUserError("缩略图尺寸不合法");
  return value as number;
}
