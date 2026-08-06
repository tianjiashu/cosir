/**
 * Markdown 流式渲染受控组件。
 *
 * 把「流式期 markdown 重解析节流」与「光标独立渲染层」从 AgentMessage 抽离为独立组件，
 * 使 markdown 渲染策略可单测、可替换（react-markdown / 未来 llm-ui 等），
 * 符合「渲染策略与展示组件解耦」的长期架构目标。
 *
 * 核心性能契约：
 * - 流式期（`streaming=true`）每帧 content 变化不立即触发 react-markdown 重解析，
 *   而是以 `REPARSE_INTERVAL_MS` 节流（默认 120ms）才把最新 content 喂给 markdown 子树；
 *   未到节流点的帧只更新「挂于独立层」的光标，不牵动 markdown 子树，避免每帧全量重 parse。
 * - 定稿期（`streaming=false`）直接渲染最新 content，无节流、无光标。
 * - 定稿态 markdown 子树用 `useMemo([renderedContent, components])` 缓存解析结果：
 *   同一 `renderedContent` + 同一 `components` 引用不重复跑 react-markdown 全量解析，
 *   使冷启动从零全量投影（父层因事件引用变化而多次重渲染）时，历史消息只解析一次，
 *   避免几十次同步全量解析阻塞主线程。流式态与 `REPARSE_INTERVAL_MS` 节流互补——
 *   仅当 `renderedContent` 真正变化时才重算，不会丢失流式 token。
 * - 光标层（StreamingCaret）独立挂载于 markdown 子树之外，位置由调用方决定。
 *
 * @module components/chat/MarkdownStream
 */

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";

/** 流式期 markdown 重解析节流间隔（毫秒）。 */
const REPARSE_INTERVAL_MS = 120;

/** MarkdownStream 组件属性。 */
interface MarkdownStreamProps {
  /** Markdown 文本内容。 */
  content: string;
  /** 是否处于流式生成中。 */
  streaming?: boolean;
  /** react-markdown 的自定义组件映射（由调用方按状态注入光标逻辑）。 */
  components: Components;
  /** 挂于 markdown 子树之外的光标层（独立渲染，不触发 markdown 重解析）。 */
  caret?: ReactNode;
  /** 额外的容器 className。 */
  className?: string;
}

/**
 * Markdown 流式渲染组件。
 *
 * 目的:
 *   在「流式实时性」与「markdown 重解析开销」之间取平衡——流式期节流重解析、
 *   光标独立层；定稿期零节流直接渲染，且用 useMemo 缓存定稿态解析结果。
 *
 * 参数:
 *   content - Markdown 文本；streaming - 是否流式；
 *   components - react-markdown 组件映射；caret - 独立光标层；
 *   className - 容器类名。
 *
 * 返回:
 *   包含节流后的 markdown 子树与独立光标层的 React 元素。
 *
 * 异常:
 *   不主动抛出。
 *
 * 副作用:
 *   流式期持有一个节流定时器（rAF/timeout），卸载时清理；
 *   定稿态把 ReactMarkdown 子树用 useMemo 缓存，仅当 renderedContent/components
 *   变化时重新解析，降低冷启动全量重投影时的重复解析开销。
 */
export function MarkdownStream({ content, streaming = false, components, caret, className }: MarkdownStreamProps) {
  // 节流后的「实际喂给 markdown 子树」的内容；初始为最新 content。
  const [renderedContent, setRenderedContent] = useState(content);
  const lastReparseAtRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 始终保存最新 content：节流定时器触发时读取此 ref，
  // 避免闭包捕获注册时刻的旧 content 导致「窗口期内到达的 token 永久丢失」。
  const latestContentRef = useRef(content);
  latestContentRef.current = content;

  useEffect(() => {
    if (!streaming) {
      // 定稿：取消待触发的节流定时器，避免它稍后用旧快照覆盖最终内容。
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      setRenderedContent(content);
      return;
    }
    const now = Date.now();
    const elapsed = now - lastReparseAtRef.current;
    if (elapsed >= REPARSE_INTERVAL_MS) {
      lastReparseAtRef.current = now;
      setRenderedContent(content);
    } else if (!timerRef.current) {
      // 未到节流点：注册一次定时器，触发时读取 ref 中的最新 content。
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        lastReparseAtRef.current = Date.now();
        setRenderedContent(latestContentRef.current);
      }, REPARSE_INTERVAL_MS - elapsed);
    }
  }, [content, streaming]);

  // 卸载或切到定稿时清理待触发的节流定时器，避免泄漏。
  useEffect(() => {
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, []);

  // 定稿态缓存：同一 renderedContent + components 不重复跑 react-markdown + remark-gfm 全量解析。
  // 流式期 renderedContent 受 REPARSE_INTERVAL_MS 节流已天然降低解析频率，此处缓存与之互补而非冲突——
  // 仅当 renderedContent 真正变化或 components 引用变化时才重算，父层因事件引用变化而重渲染时直接复用旧结果。
  const renderedMarkdown = useMemo(
    () => (
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {renderedContent}
      </ReactMarkdown>
    ),
    [renderedContent, components],
  );

  return (
    <div className={className}>
      {renderedMarkdown}
      {streaming ? caret : null}
    </div>
  );
}
