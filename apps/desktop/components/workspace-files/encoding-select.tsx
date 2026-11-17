import { NativeSelect } from "@/components/ui/native-select";

/** 可选的文本编码（后端 `ENCODINGS` 的子集）；当前编码不在其中时排在最前。 */
const ENCODINGS = ["utf-8", "utf-8-sig", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be", "gb18030", "big5", "shift_jis", "cp1252"];

/** 文本阅读器工具栏里的编码选择，源码 / Markdown 阅读器与表格阅读器共用。 */
export function EncodingSelect({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <NativeSelect aria-label="文本编码" size="sm" wrapperClassName="w-32" value={value} onChange={(event) => onChange(event.target.value)}>
      {[...new Set([value, ...ENCODINGS])].map((option) => <option key={option}>{option}</option>)}
    </NativeSelect>
  );
}
