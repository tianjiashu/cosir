import { useState } from "react";

export type FieldEdits<T extends object> = {
  /** 表单显示的值：服务端最新值叠上用户改过的字段。 */
  values: T;
  /** 动过且与服务端最新值不同的字段：保存时只提交它们。 */
  changes: Partial<T>;
  dirty: boolean;
  /** 接收整份表单值（表单组件通常整份回传），只把与当前显示值不同的字段记为改动。 */
  edit: (next: T) => void;
};

/**
 * 配置表单的编辑副本：只记用户改过的字段，其余字段始终跟随服务端最新值。
 *
 * 为什么不在打开时整份拷贝：配置文件可以在外部改（手改、git、Agent），列表随时会重新读取。整份拷贝要么让
 * 没碰过的字段显示旧值，要么拿旧值当基准算差异，把没碰过的字段当成修改写回去，悄悄覆盖外部改动。只记改动时，
 * 外部改的字段与用户改的字段各自保留；两边改了同一个字段以用户为准（单用户本机应用可接受的取舍）。
 *
 * `equal` 决定「改了但和服务端一样」算不算改动（如忽略首尾空白），缺省为严格相等。
 * 环境变量面板不用它：密钥只写不读、清空是显式操作，那是另一种契约（见 `environment-panel.tsx`）。
 */
export function useFieldEdits<T extends object>(
  live: T,
  equal: (edited: unknown, current: unknown) => boolean = Object.is,
): FieldEdits<T> {
  const [edits, setEdits] = useState<Partial<T>>({});
  const values = { ...live, ...edits } as T;
  const changes = Object.fromEntries(
    Object.entries(edits).filter(([key, value]) => !equal(value, live[key as keyof T])),
  ) as Partial<T>;

  const edit = (next: T) => {
    const changed = (Object.keys(next) as (keyof T)[]).filter((key) => !Object.is(next[key], values[key]));
    if (changed.length === 0) return;
    setEdits((current) => ({ ...current, ...Object.fromEntries(changed.map((key) => [key, next[key]])) }));
  };

  return { values, changes, dirty: Object.keys(changes).length > 0, edit };
}
