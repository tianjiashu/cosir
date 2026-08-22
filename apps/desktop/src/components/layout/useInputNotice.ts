/**
 * 输入框 notice 通道（统一提示承载）。
 *
 * 借鉴 deepseek-harness 的 InputNotice 设计（``docs/输入组件优化.md`` §5.5/§8.5）：
 * 提示与草稿/phase 解耦，携带级别与单调序号，经 ``role="status" aria-live`` 区域
 * 向读屏播报。
 *
 * 职责边界：
 * - 本 hook 只负责输入框区域「一条当前提示」的承载与清除。
 * - 提示的**产生**（guard 拦截/发送失败）由 ``useSendInput`` 调用本 hook 完成；
 *   提示的**渲染**由 InputBar 完成；seq 仅供测试断言与强制重渲染，不参与业务逻辑。
 *
 * @module components/layout/useInputNotice
 */

import { useCallback, useRef, useState } from "react";

/** 提示级别：error（拦截/失败）| info（中性引导）。 */
export type InputNoticeLevel = "error" | "info";

/** 一条输入框提示。 */
export interface InputNotice {
  /** 级别，决定渲染配色（error → destructive 文本）。 */
  level: InputNoticeLevel;
  /** 面向用户的提示文案（中文）。 */
  text: string;
  /** 单调递增序号：即使文案相同也能区分「新提示」，供测试断言与强制重渲染。 */
  seq: number;
}

/** useInputNotice 返回值。 */
export interface UseInputNoticeReturn {
  /** 当前提示；无提示时为 null。 */
  notice: InputNotice | null;
  /** 展示一条提示（覆盖旧提示，seq 自增）。 */
  showNotice: (level: InputNoticeLevel, text: string) => void;
  /** 清除当前提示。 */
  clearNotice: () => void;
}

/**
 * 输入框 notice hook。
 *
 * @returns 当前提示与展示/清除动作。
 */
export function useInputNotice(): UseInputNoticeReturn {
  const [notice, setNotice] = useState<InputNotice | null>(null);
  const seqRef = useRef(0);

  const showNotice = useCallback((level: InputNoticeLevel, text: string) => {
    seqRef.current += 1;
    setNotice({ level, text, seq: seqRef.current });
  }, []);

  const clearNotice = useCallback(() => {
    setNotice(null);
  }, []);

  return { notice, showNotice, clearNotice };
}
