import { useState } from "react";

import { NativeSelect } from "@/components/ui/native-select";

/** 可选的文本编码（后端 `ENCODINGS` 的子集）；当前编码不在其中时排在最前。 */
const ENCODINGS = ["utf-8", "utf-8-sig", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be", "gb18030", "big5", "shift_jis", "cp1252"];

const label = (encoding: string) => encoding.toUpperCase();

/**
 * 「更多」底部文件信息里的编码（源码 / Markdown 阅读器与表格阅读器共用）。
 *
 * 编码由后端打开文件时识别（BOM、UTF-8 合法性、`charset_normalizer`），绝大多数文件不需要用户碰它；这里只是
 * 看到乱码时的兜底：平时只说当前编码与来源，点「更改」才出下拉。手动选过的编码记在这个文件的阅读位置里
 * （`encoding`，只在内存、标签开着期间有效，关掉标签或重启后回到识别结果），「恢复自动」清掉它。
 */
export function EncodingControl({ encoding, detected, manual, onChange, onReset }: {
  encoding: string;
  /** 后端识别的编码；null 表示没识别出来（按 UTF-8 显示）。 */
  detected: string | null;
  manual: boolean;
  onChange: (encoding: string) => void;
  onReset: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const source = manual ? "手动选择" : detected ? "自动识别" : "未识别";
  if (!editing && !manual) {
    return (
      <p className="flex items-center gap-1.5">
        <span>编码 {label(encoding)} · {source}</span>
        <button type="button" className="text-foreground underline-offset-2 hover:underline" onClick={() => setEditing(true)}>更改</button>
      </p>
    );
  }
  return (
    <div className="flex items-center gap-1.5">
      <span className="shrink-0">编码</span>
      <NativeSelect aria-label="文本编码" size="sm" wrapperClassName="w-32" value={encoding} onChange={(event) => onChange(event.target.value)}>
        {[...new Set([encoding, ...ENCODINGS])].map((option) => <option key={option} value={option}>{label(option)}</option>)}
      </NativeSelect>
      {manual && (
        <button type="button" className="text-foreground shrink-0 underline-offset-2 hover:underline"
          onClick={() => { setEditing(false); onReset(); }}>恢复自动</button>
      )}
    </div>
  );
}
