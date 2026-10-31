import { useEffect, useRef, useState } from "react";

/**
 * 为一组本地 `File` 维护对象 URL：新出现的文件建一个，离开列表的立即释放，卸载时全部释放。
 *
 * 在图片组这一层建一次，缩略图与灯箱共用同一个地址；`files` 的引用变化才重新对账，调用方应传入
 * 稳定的数组（store 里的列表或 `useMemo` 结果）。未建好之前（首帧）对应文件没有地址，缩略图显示占位。
 */
export function useObjectUrls(files: readonly File[]): ReadonlyMap<File, string> {
  const cache = useRef(new Map<File, string>());
  const [urls, setUrls] = useState<ReadonlyMap<File, string>>(() => new Map());

  useEffect(() => {
    const current = cache.current;
    for (const [file, url] of current) {
      if (!files.includes(file)) {
        URL.revokeObjectURL(url);
        current.delete(file);
      }
    }
    for (const file of files) {
      if (!current.has(file)) current.set(file, URL.createObjectURL(file));
    }
    setUrls(new Map(current));
  }, [files]);

  useEffect(() => () => {
    for (const url of cache.current.values()) URL.revokeObjectURL(url);
    cache.current.clear();
  }, []);

  return urls;
}
