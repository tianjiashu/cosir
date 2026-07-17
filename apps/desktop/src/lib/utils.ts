/**
 * UI 工具纯函数集合。
 *
 * 仅放与 UI 无关的纯函数（如 class 合并）。
 * 组件级逻辑一律放 hooks/，不在此处。
 *
 * @module lib/utils
 */

import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * 合并 Tailwind CSS 类名。
 *
 * 使用 clsx 处理条件类名，再用 tailwind-merge 解决 Tailwind 类名冲突
 * （后声明的同类属性覆盖先声明的）。
 *
 * @param inputs - 可变的类名输入，支持字符串、对象、数组等 clsx 格式。
 * @returns 合并后的最终类名字符串。
 *
 * @example
 * ```ts
 * cn("px-2 py-1", is_active && "bg-blue-500", { "opacity-50": disabled })
 * // => "py-1 px-2 bg-blue-500 opacity-50"（顺序经 tailwind-merge 优化）
 * ```
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/**
 * 从路径中提取末级目录名（跨平台）。
 *
 * 用于把用户选择的目录绝对路径转换为工作区名称。
 * 同时兼容 Unix（`/`）与 Windows（`\\`）路径分隔符。
 *
 * @param path - 本地目录绝对路径。
 * @returns 末级目录名；路径为空或无法解析时回退为原路径。
 */
export function basenameOf(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1] : path;
}
