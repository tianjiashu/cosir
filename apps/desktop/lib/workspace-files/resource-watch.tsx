import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";

/** Markdown 引用资源与主预览共享一个监听连接，重复图片按引用计数释放。 */
export const ResourceWatchContext = createContext<
  ((path: string) => () => void) | null
>(null);
export function useResourceWatchTargets() {
  const counts = useRef(new Map<string, number>());
  const [paths, setPaths] = useState<string[]>([]);
  const register = useCallback((path: string) => {
    const previous = counts.current.get(path) ?? 0;
    counts.current.set(path, previous + 1);
    if (!previous) setPaths([...counts.current.keys()]);
    return () => {
      const remaining = counts.current.get(path)! - 1;
      if (remaining) counts.current.set(path, remaining);
      else {
        counts.current.delete(path);
        setPaths([...counts.current.keys()]);
      }
    };
  }, []);
  return { paths, register };
}
export function useResourceWatch(path: string) {
  const register = useContext(ResourceWatchContext);
  if (!register) throw new Error("文件资源必须挂在工作区阅读器中");
  useEffect(() => register(path), [register, path]);
}
