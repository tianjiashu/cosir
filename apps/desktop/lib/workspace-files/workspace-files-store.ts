import type { LineRange } from "@/lib/links/resolve-link";
import { create } from "zustand";

type FileTab = { path: string; pinned: boolean };
export type ReadingPosition = {
  offset: number;
  before?: number;
  tail?: boolean;
  scrollTop: number;
  encoding?: string;
  previewScrollTop?: number;
  /** 待定位的行范围（链接带行号打开时写入）；文本预览选中并居中后清除，之后照常按 `scrollTop` 恢复。 */
  reveal?: LineRange;
  /** 待定位的 PDF 页码（引用带页码打开时写入）；PDF 预览跳转后清除。 */
  revealPage?: number;
};
/** 有渲染形态的文件（Markdown、SVG、无内置预览的二进制）看渲染结果还是源码；纯文本只有源码。 */
export type ReadingView = "rendered" | "source";
export type ReadingPreference = {
  wrap?: boolean;
  view?: ReadingView;
};
export type FileSession = {
  preferences: Record<string, ReadingPreference>;
  tabs: FileTab[];
  active: string | null;
  expanded: string[];
  positions: Record<string, ReadingPosition>;
  treeWidth: number;
  treeVisible: boolean;
  browsing: boolean;
  /** 目录栏文件名搜索框里的文字；改动即撤销已提交的搜索，按 Enter 才提交。 */
  searchText: string;
  /**
   * 已提交的文件名搜索；`id` 让同一个词再搜一次也重新开始。放在会话里而不是面板组件里：用户显式选择查找时，打开入口可发起「查找同名文件」（`searchFilename`）。
   */
  searchRequest: { query: string; id: number; navigation?: { lines?: LineRange } } | null;
};
const fresh = (): FileSession => ({
  preferences: {},
  tabs: [],
  active: null,
  expanded: [""],
  positions: {},
  treeWidth: 230,
  treeVisible: true,
  browsing: true,
  searchText: "",
  searchRequest: null,
});
const emptySession = fresh();
export const useFilesStore = create<{
  /** 用户导航的单调序号，包括重新选择同一文件；异步打开只提交最新意图。 */
  navigationId: number;
  beginNavigation: () => number;
  sessions: Record<number, FileSession>;
  patch: (id: number, patch: Partial<FileSession>) => void;
  open: (id: number, path: string, pinned?: boolean) => void;
  close: (id: number, path: string) => void;
  forget: (id: number) => void;
  preference: (id: number, path: string, patch: ReadingPreference) => void;
  position: (id: number, path: string, patch: Partial<ReadingPosition>) => void;
  /** 提交一次文件名搜索并亮出目录栏（窄面板时从阅读切到浏览）。 */
  searchFilename: (id: number, query: string, navigation?: { lines?: LineRange }) => void;
}>((set, get) => ({
  navigationId: 0,
  beginNavigation: () => {
    const navigationId = get().navigationId + 1;
    set({ navigationId });
    return navigationId;
  },
  sessions: {},
  patch: (id, patch) =>
    set((state) => ({
      navigationId: "searchRequest" in patch ? state.navigationId + 1 : state.navigationId,
      sessions: {
        ...state.sessions,
        [id]: { ...(state.sessions[id] ?? fresh()), ...patch },
      },
    })),
  open: (id, path, pinned = false) =>
    set((state) => {
      const session = state.sessions[id] ?? fresh();
      const existing = session.tabs.find((tab) => tab.path === path);
      const tabs = existing
        ? session.tabs.map((tab) =>
            tab.path === path ? { ...tab, pinned: tab.pinned || pinned } : tab,
          )
        : [...session.tabs.filter((tab) => tab.pinned), { path, pinned }];
      const positions = Object.fromEntries(
        Object.entries(session.positions).filter(([key]) =>
          tabs.some((tab) => tab.path === key),
        ),
      );
      return {
        navigationId: state.navigationId + 1,
        sessions: {
          ...state.sessions,
          [id]: { ...session, tabs, active: path, positions, browsing: false },
        },
      };
    }),
  close: (id, path) =>
    set((state) => {
      const session = state.sessions[id] ?? fresh();
      const index = session.tabs.findIndex((tab) => tab.path === path);
      const tabs = session.tabs.filter((tab) => tab.path !== path);
      const positions = { ...session.positions };
      delete positions[path];
      return {
        navigationId: state.navigationId + 1,
        sessions: {
          ...state.sessions,
          [id]: {
            ...session,
            tabs,
            positions,
            active:
              session.active === path
                ? (tabs[Math.min(index, tabs.length - 1)]?.path ?? null)
                : session.active,
          },
        },
      };
    }),
  forget: (id) =>
    set((state) => {
      const sessions = { ...state.sessions };
      delete sessions[id];
      return { sessions };
    }),
  // 阅读选择属于工作区会话偏好，关闭标签仍保留；不写回后端文件事实。
  preference: (id, path, patch) =>
    set((state) => {
      const session = state.sessions[id] ?? fresh();
      return {
        sessions: {
          ...state.sessions,
          [id]: {
            ...session,
            preferences: {
              ...session.preferences,
              [path]: { ...session.preferences[path], ...patch },
            },
          },
        },
      };
    }),
  searchFilename: (id, query, navigation) =>
    set((state) => {
      const session = state.sessions[id] ?? fresh();
      return {
        navigationId: state.navigationId + 1,
        sessions: {
          ...state.sessions,
          [id]: {
            ...session,
            searchText: query,
            searchRequest: { query, id: (session.searchRequest?.id ?? 0) + 1, navigation },
            treeVisible: true,
            browsing: true,
          },
        },
      };
    }),
  position: (id, path, patch) =>
    set((state) => {
      const session = state.sessions[id] ?? fresh();
      return {
        sessions: {
          ...state.sessions,
          [id]: {
            ...session,
            positions: {
              ...session.positions,
              [path]: {
                ...(session.positions[path] ?? {
                  offset: 0,
                  scrollTop: 0,
                }),
                ...patch,
              },
            },
          },
        },
      };
    }),
}));
export const fileSession = (
  sessions: Record<number, FileSession>,
  id: number,
) => sessions[id] ?? emptySession;

/**
 * 复制给用户的路径文本。工作区内路径统一用 `/`；绝对路径按工作区根的分隔符拼接，Windows 根
 * （含 `\`）下得到原生的 `C:\repo\src` 形式。根目录的相对路径写作 `.`。
 */
export function workspacePathText(rootPath: string, path: string, absolute: boolean): string {
  if (!absolute) return path || ".";
  const separator = rootPath.includes("\\") ? "\\" : "/";
  const root = rootPath.replace(/[\\/]$/, "");
  return path ? `${root}${separator}${path.replaceAll("/", separator)}` : root;
}

export function parentPath(path: string): string {
  return path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
}
export function parentsOf(path: string): string[] {
  const parts = path.split("/");
  return [
    "",
    ...parts
      .slice(0, -1)
      .map((_, index) => parts.slice(0, index + 1).join("/")),
  ];
}
