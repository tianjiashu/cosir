import { useEffect, useState } from "react";

const SCRAMBLE_GLYPHS = "!<>-_\\/[]{}=+*^?#%&@$01";
const FRAME_MS = 45;

export type DecodedGlyph = { char: string; settled: boolean };

function settledGlyphs(text: string): DecodedGlyph[] {
  return Array.from(text, (char) => ({ char, settled: true }));
}

/** 解码开始前用不换行空格占位：等宽字体下宽度不变，字标不会在解码开始时跳动。 */
function pendingGlyphs(text: string): DecodedGlyph[] {
  return Array.from(text, () => ({ char: " ", settled: false }));
}

function randomGlyph(): string {
  return SCRAMBLE_GLYPHS[Math.floor(Math.random() * SCRAMBLE_GLYPHS.length)];
}

/**
 * 「解码」式文字入场：`delayMs` 后各位先滚动随机符号，再在 `durationMs` 内从左到右逐位定格成原文。
 * 纯展示效果，只用于启动页字标；`disabled`（如系统要求减少动态效果）时直接返回原文，不起计时器。
 */
export function useDecodedText(
  text: string,
  { delayMs, durationMs, disabled }: { delayMs: number; durationMs: number; disabled: boolean },
): DecodedGlyph[] {
  const [glyphs, setGlyphs] = useState(() => pendingGlyphs(text));

  useEffect(() => {
    if (disabled) return;
    const startedAt = performance.now();
    const chars = Array.from(text);
    const timer = window.setInterval(() => {
      const elapsed = performance.now() - startedAt - delayMs;
      if (elapsed < 0) return;
      if (elapsed >= durationMs) {
        setGlyphs(settledGlyphs(text));
        window.clearInterval(timer);
        return;
      }
      const settledCount = Math.floor((elapsed / durationMs) * chars.length);
      setGlyphs(chars.map((char, index) => (
        index < settledCount ? { char, settled: true } : { char: randomGlyph(), settled: false }
      )));
    }, FRAME_MS);
    return () => window.clearInterval(timer);
  }, [text, delayMs, durationMs, disabled]);

  return disabled ? settledGlyphs(text) : glyphs;
}
