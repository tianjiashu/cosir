/**
 * 终端只读输出查看器（xterm.js）。
 *
 * 职责单一：把「命令运行期的累积输出文本」渲染成带 ANSI 能力的滚动终端视图。
 * 只做单向只读展示——不创建 PTY、不接管 stdin、不向后端写入任何数据。
 *
 * 设计要点：
 * - 增量写入：只把 `output` 相对上一帧新增的尾部切片写入 xterm，避免每帧全量重绘。
 * - 回退（output 变短/被重置）时清屏重写，保证视图与 props 一致（幂等）。
 * - 组件仅在命令 `running` 期间挂载；终态由调用方切回静态 `<pre>` 渲染，
 *   因此本组件无需处理「终态 output 被清空导致清屏」的竞态。
 *
 * @module components/chat/TerminalViewer
 */

import * as React from "react";
import { memo } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { cn } from "@/lib/utils";
import { logWarn } from "@/lib/logger";

/** TerminalViewer 组件属性。 */
interface TerminalViewerProps {
  /** 命令的累积输出全文（调用方负责累积，本组件只做增量渲染）。 */
  output: string;
  /** 额外样式类。 */
  className?: string;
}

/** 终端视图行数上限：限制回滚缓冲，避免长命令输出撑爆内存。 */
const SCROLLBACK_LINES = 2000;

/**
 * 安全执行 xterm 尺寸自适应。
 *
 * 在测试环境（happy-dom / jsdom）中容器无真实尺寸，`fit()` 可能抛错；
 * 此处吞掉异常但记录 warn 日志，避免渲染链路因布局细节中断。
 *
 * 参数:
 *   fitAddon - 已加载到 Terminal 的 FitAddon 实例。
 *
 * 返回:
 *   无。
 *
 * 异常:
 *   不抛出；内部异常经 `logWarn` 记录。
 *
 * 副作用:
 *   可能改变 xterm 的 cols/rows。
 */
function safeFit(fitAddon: FitAddon): void {
  try {
    fitAddon.fit();
  } catch (err) {
    logWarn("terminal_viewer_fit_failed", {
      module: "TerminalViewer",
      reason: err instanceof Error ? err.message : String(err),
    });
  }
}

/**
 * TerminalViewer：命令运行期的只读输出终端。
 *
 * 挂载时创建 xterm 实例并 `open` 到容器，卸载时 `dispose` 释放；
 * `output` 变化时只写入新增尾部切片并滚动到底部。
 */
export const TerminalViewer = memo(function TerminalViewer({ output, className }: TerminalViewerProps) {
  const containerRef = React.useRef<HTMLDivElement | null>(null);
  const termRef = React.useRef<Terminal | null>(null);
  const fitRef = React.useRef<FitAddon | null>(null);
  /** 已写入 xterm 的 output 前缀长度，用于计算增量切片。 */
  const writtenLenRef = React.useRef(0);

  React.useEffect(() => {
    const container = containerRef.current;
    if (!container) {
      return;
    }
    const term = new Terminal({
      disableStdin: true,
      convertEol: true,
      cursorBlink: false,
      cursorStyle: "bar",
      scrollback: SCROLLBACK_LINES,
      fontSize: 11,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
      theme: { background: "#00000000" },
      allowTransparency: true,
    });
    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.open(container);
    safeFit(fitAddon);

    termRef.current = term;
    fitRef.current = fitAddon;
    writtenLenRef.current = 0;

    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => safeFit(fitAddon)) : null;
    observer?.observe(container);

    return () => {
      observer?.disconnect();
      termRef.current = null;
      fitRef.current = null;
      term.dispose();
    };
  }, []);

  React.useEffect(() => {
    const term = termRef.current;
    if (!term) {
      return;
    }
    const written = writtenLenRef.current;
    // output 回退（重置/换调用）时整体重写，保证视图与 props 严格一致。
    if (output.length < written || !output.startsWith(output.slice(0, written))) {
      term.clear();
      writtenLenRef.current = 0;
    }
    const delta = output.slice(writtenLenRef.current);
    if (delta.length === 0) {
      return;
    }
    term.write(delta);
    writtenLenRef.current = output.length;
    term.scrollToBottom();
  }, [output]);

  return <div ref={containerRef} className={cn("h-64 w-full overflow-hidden", className)} />;
});
