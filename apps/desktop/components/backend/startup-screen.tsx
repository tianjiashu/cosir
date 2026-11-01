import { useEffect, useState } from "react";

import { CosirMark } from "@/components/cosir-mark";
import { useMediaQuery } from "@/hooks/use-media-query";
import { useDecodedText } from "./use-decoded-text";
import "./startup-screen.css";

type BackendStartupScreenProps = {
  exiting?: boolean;
};

const WORDMARK = "COSIR";

/** 自挂载起的等待秒数，`running` 为 false 时停表。只显示真实等待时长，不推断启动阶段。 */
function useElapsedSeconds(running: boolean): number {
  const [startedAt] = useState(() => performance.now());
  const [now, setNow] = useState(startedAt);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(performance.now()), 100);
    return () => window.clearInterval(timer);
  }, [running]);
  return (now - startedAt) / 1000;
}

/**
 * 桌面启动门禁：宿主启动并检查本机后端期间的全屏遮罩，画面是终端 / CRT 开机风格。
 * 不承担任何进程或网络工作；`exiting` 只在调用方确认后端就绪后播放关机式退场。
 *
 * 宿主快照只有 starting / ready / failed，没有更细的启动阶段，所以这里只显示真实等待时长，
 * 不编造逐行启动日志。调用方须让同一实例从 starting 持续到 exiting（见 `src/App.tsx`），
 * 否则退场时会重新挂载，入场动画、解码与计时都从头再来。
 */
export function BackendStartupScreen({ exiting = false }: BackendStartupScreenProps) {
  const reducedMotion = useMediaQuery("(prefers-reduced-motion: reduce)");
  const wordmark = useDecodedText(WORDMARK, { delayMs: 500, durationMs: 550, disabled: reducedMotion });
  const elapsed = useElapsedSeconds(!exiting);

  return (
    <div
      // 隐藏标题栏后，启动期间整屏都可拖动窗口。
      className="backend-startup-screen app-drag-region"
      data-state={exiting ? "exiting" : "starting"}
      aria-busy={!exiting}
      aria-hidden={exiting || undefined}
    >
      <div className="backend-startup-screen__crt">
        <section
          className="backend-startup-screen__content"
          role={exiting ? undefined : "status"}
          aria-live={exiting ? undefined : "polite"}
        >
          <div className="backend-startup-screen__mark-wrap" aria-hidden="true">
            <span className="backend-startup-screen__corner backend-startup-screen__corner--tl" />
            <span className="backend-startup-screen__corner backend-startup-screen__corner--tr" />
            <span className="backend-startup-screen__corner backend-startup-screen__corner--bl" />
            <span className="backend-startup-screen__corner backend-startup-screen__corner--br" />
            <CosirMark className="backend-startup-screen__mark" />
          </div>
          <h1 className="backend-startup-screen__wordmark" aria-label={WORDMARK}>
            {wordmark.map((glyph, index) => (
              <span key={index} aria-hidden="true" data-settled={glyph.settled || undefined}>{glyph.char}</span>
            ))}
          </h1>
          <p className="backend-startup-screen__status">
            <span className="backend-startup-screen__prompt" aria-hidden="true">&gt;</span>
            <span>正在启动本机 Agent</span>
            <span className="backend-startup-screen__block-cursor" aria-hidden="true" />
            <span className="backend-startup-screen__elapsed" aria-hidden="true">[{elapsed.toFixed(1)}s]</span>
          </p>
        </section>
      </div>
      <div className="backend-startup-screen__beam" aria-hidden="true" />
    </div>
  );
}
