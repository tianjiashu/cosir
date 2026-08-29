/**
 * 附件输入状态与校验 hook（输入层附件子域）。
 *
 * 单一职责：维护 InputBar 附件列表的生命周期（增 / 删 / 清空）、按本地路径或
 * URL 生成正确的 ``AttachmentRef``、在发送前做与后端契约一致的前置校验
 * （数量 / ref 非空 / ref 长度 / URL 协议 / 当前模型视觉能力）。
 *
 * 职责边界：
 * - 本文件只负责「附件数据 + 校验」，不渲染 UI（渲染交给 ``AttachmentChip.tsx``）。
 * - 不负责发送事务本身（guard/创建任务或轮次由 ``useSendInput`` 承担）。
 * - 不感知运行态互斥锁；仅暴露纯函数式校验，供 InputBar 在调用 ``send`` 前阻断。
 *
 * 后端契约来源（``apps/backend/app/api/schemas/request/CreateTaskRequest.py`` 与
 * ``CreateTurnRequest.py`` + ``service/task/turn_service.py``）：
 * - 附件可选；传入则不能为空数组；最多 20 个（``MAX_ATTACHMENTS=20``）。
 * - 每个 ``ref`` 去空白后非空且长度 ≤4096（``MAX_REF_LEN=4096``）。
 * - ``url`` 类型 ``ref`` 必须以 ``http://`` 或 ``https://`` 开头。
 * - 模型 ``supports_image=false`` 但带 image 附件 → 后端抛 ``VisionNotSupportedError``（HTTP 422）；
 *   此处做前端拦截，避免无效请求（与 ``useModelSendGuard`` 的模型层校验解耦）。
 *
 * @module components/layout/useAttachmentInput
 */

import { useCallback, useState } from "react";
import type { AttachmentKind, AttachmentRef } from "@shared/attachment";
import { logWarn } from "@/lib/logger";

/** 附件数量上限，与后端 ``MAX_ATTACHMENTS=20`` 对齐。 */
export const ATTACHMENT_MAX_COUNT = 20;

/** 单个附件 ref 长度上限，与后端 ``MAX_REF_LEN=4096`` 对齐。 */
export const ATTACHMENT_MAX_REF_LEN = 4096;

/** 图片扩展名集合（判定 image / file 用）。 */
const IMAGE_EXTENSIONS = new Set([
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".bmp",
  ".svg",
]);

/**
 * 发送前附件校验结果。
 */
export interface AttachmentValidationResult {
  /** 是否通过（可发送）。 */
  ok: boolean;
  /** 未通过时的面向用户中文提示；通过时为 undefined。 */
  message?: string;
}

/**
 * 推断本地路径或 URL 的附件类型。
 *
 * 规则：以 ``http://`` / ``https://`` 开头视为 ``url``；按扩展名命中图片集合为
 * ``image``；其余按 ``file`` 处理（目录由 ``addDirectoryPath`` 显式标记，不走此推断）。
 *
 * @param path - 本地绝对路径或 URL。
 * @returns 推断出的附件类型。
 */
export function inferAttachmentKind(path: string): AttachmentKind {
  const normalized = path.trim().toLowerCase();
  if (normalized.startsWith("http://") || normalized.startsWith("https://")) {
    return "url";
  }
  const dotIndex = normalized.lastIndexOf(".");
  const extension = dotIndex >= 0 ? normalized.slice(dotIndex) : "";
  return IMAGE_EXTENSIONS.has(extension) ? "image" : "file";
}

/**
 * 按 ref 合并附件并去重，保持原有顺序优先（后选不覆盖先选）。
 *
 * @param current - 当前附件列表。
 * @param incoming - 新加入的附件列表。
 * @returns 合并去重后的附件列表。
 */
export function mergeAttachments(current: AttachmentRef[], incoming: AttachmentRef[]): AttachmentRef[] {
  const seen = new Set<string>();
  const merged: AttachmentRef[] = [];
  for (const attachment of [...current, ...incoming]) {
    if (seen.has(attachment.ref)) {
      continue;
    }
    seen.add(attachment.ref);
    merged.push(attachment);
  }
  return merged;
}

/**
 * 由本地文件/图片路径构造附件引用（kind 经扩展名推断）。
 *
 * @param path - Tauri dialog 返回的文件绝对路径。
 * @returns 可提交给后端的附件引用。
 */
export function pathToAttachment(path: string): AttachmentRef {
  return {
    kind: inferAttachmentKind(path),
    ref: path,
  };
}

/**
 * 由本地目录路径构造附件引用（kind 恒为 directory）。
 *
 * @param path - Tauri dialog 返回的目录绝对路径。
 * @returns 可提交给后端的附件引用。
 */
export function directoryToAttachment(path: string): AttachmentRef {
  return {
    kind: "directory",
    ref: path,
  };
}

/**
 * 由用户输入的 URL 构造附件引用（kind 恒为 url；协议校验由调用方/validateForSend 负责）。
 *
 * @param url - 用户输入或粘贴的 URL（允许首尾空白，内部 trim）。
 * @returns 可提交给后端的附件引用。
 */
export function urlToAttachment(url: string): AttachmentRef {
  return {
    kind: "url",
    ref: url.trim(),
  };
}

/**
 * 附件输入 hook 返回值。
 */
export interface UseAttachmentInputReturn {
  /** 当前待发送附件列表。 */
  attachments: AttachmentRef[];
  /** 批量加入本地文件/图片路径（image/file 经扩展名自动推断，合并去重）。 */
  addLocalFilePaths: (paths: string[]) => void;
  /** 加入单个本地目录（kind=directory）。 */
  addDirectoryPath: (path: string) => void;
  /** 加入单个 URL（kind=url；空值忽略）。 */
  addUrl: (url: string) => void;
  /** 按 ref 移除单个附件。 */
  removeAttachment: (ref: string) => void;
  /** 清空全部附件（发送成功后调用）。 */
  clear: () => void;
  /**
   * 发送失败回滚：用快照恢复附件列表（保留顺序与内容）。
   *
   * @param restored - 发送前快照的附件列表。
   */
  setRestoredAttachments: (restored: AttachmentRef[]) => void;
  /** 当前附件是否含 image 类型。 */
  hasImage: () => boolean;
  /**
   * 发送前校验，对齐后端 CreateTaskRequest/CreateTurnRequest 约束。
   *
   * @param selectedSupportsImage - 当前选中模型的 supports_image（undefined 视为未声明）。
   * @returns 校验结果；失败时含中文提示，调用方应阻断发送并展示 notice。
   */
  validateForSend: (selectedSupportsImage: boolean | undefined) => AttachmentValidationResult;
}

/**
 * 附件输入 hook。
 *
 * 管理附件列表与发送前校验；校验规则全部与后端契约对齐（见文件头）。
 * 校验失败经 ``logWarn`` 留痕（带附件摘要，不落全文 ref）以便排查。
 *
 * @returns 附件状态与增删/校验动作。
 */
export function useAttachmentInput(): UseAttachmentInputReturn {
  const [attachments, setAttachments] = useState<AttachmentRef[]>([]);

  const addLocalFilePaths = useCallback((paths: string[]) => {
    if (paths.length === 0) {
      return;
    }
    setAttachments((current) => mergeAttachments(current, paths.map(pathToAttachment)));
  }, []);

  const addDirectoryPath = useCallback((path: string) => {
    if (!path) {
      return;
    }
    setAttachments((current) => mergeAttachments(current, [directoryToAttachment(path)]));
  }, []);

  const addUrl = useCallback((url: string) => {
    const trimmed = url.trim();
    if (!trimmed) {
      return;
    }
    setAttachments((current) => mergeAttachments(current, [urlToAttachment(trimmed)]));
  }, []);

  const removeAttachment = useCallback((ref: string) => {
    setAttachments((current) => current.filter((item) => item.ref !== ref));
  }, []);

  const clear = useCallback(() => {
    setAttachments([]);
  }, []);

  const setRestoredAttachments = useCallback((restored: AttachmentRef[]) => {
    setAttachments(restored);
  }, []);

  const hasImage = useCallback(() => {
    return attachments.some((item) => item.kind === "image");
  }, [attachments]);

  const validateForSend = useCallback(
    (selectedSupportsImage: boolean | undefined): AttachmentValidationResult => {
      // 规则 1：数量上限（空数组合法，仅非空时校验 ≤20）。
      if (attachments.length > ATTACHMENT_MAX_COUNT) {
        logWarn("附件数量超过上限，发送被拦截", {
          module: "InputBar",
          reason: "too_many_attachments",
          count: attachments.length,
          max: ATTACHMENT_MAX_COUNT,
        });
        return { ok: false, message: `附件数量不能超过 ${ATTACHMENT_MAX_COUNT} 个` };
      }

      // 规则 2：每个 ref 去空白后非空且长度 ≤4096；url 必须 http(s)。
      for (const item of attachments) {
        const ref = item.ref.trim();
        if (ref.length === 0) {
          logWarn("存在空附件引用，发送被拦截", {
            module: "InputBar",
            reason: "empty_ref",
            kind: item.kind,
          });
          return { ok: false, message: "附件路径不能为空" };
        }
        if (item.ref.length > ATTACHMENT_MAX_REF_LEN) {
          logWarn("附件引用超长，发送被拦截", {
            module: "InputBar",
            reason: "ref_too_long",
            kind: item.kind,
            ref_len: item.ref.length,
            max: ATTACHMENT_MAX_REF_LEN,
          });
          return { ok: false, message: `附件路径长度不能超过 ${ATTACHMENT_MAX_REF_LEN} 个字符` };
        }
        if (item.kind === "url" && !/^https?:\/\//i.test(ref)) {
          logWarn("URL 附件协议非法，发送被拦截", {
            module: "InputBar",
            reason: "invalid_url_scheme",
            ref_preview: ref.slice(0, 64),
          });
          return { ok: false, message: "URL 必须以 http(s):// 开头" };
        }
      }

      // 规则 3：当前模型明确不支持视觉且带 image 附件 → 前端拦截。
      if (selectedSupportsImage === false && hasImage()) {
        logWarn("当前模型不支持图片附件，发送被拦截", {
          module: "InputBar",
          reason: "vision_not_supported",
          supports_image: selectedSupportsImage,
        });
        return { ok: false, message: "当前模型不支持图片附件，请更换支持视觉的模型" };
      }

      return { ok: true };
    },
    [attachments, hasImage],
  );

  return {
    attachments,
    addLocalFilePaths,
    addDirectoryPath,
    addUrl,
    removeAttachment,
    clear,
    setRestoredAttachments,
    hasImage,
    validateForSend,
  };
}
