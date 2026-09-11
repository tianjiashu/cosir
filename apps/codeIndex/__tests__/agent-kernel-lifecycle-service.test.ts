/**
 * agent-kernel lifecycle-service 单元测试（对齐方案一 L3 验收）。
 *
 * 验证 LifecycleService 的核心契约：
 * - `handle` 对非生命周期方法 / 缺 `workspace_path` 抛 INVALID_REQUEST；
 * - `codegraph_status` 在无索引根时返回 unindexed（不打开实例）；
 * - 归一化状态映射（complete/partial → ready）；
 * - `init` 对已初始化目录走 open+indexAll、未初始化走 CodeGraph.init。
 *
 * 用 vi.mock 替代真实 CodeGraph 主类与 directory，避免依赖真实索引与 node 运行时。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

// 先 mock 依赖，再导入被测模块（vi.mock 会提升到导入之前）。
const { findNearestCodeGraphRoot, isInitialized } = await import('../src/directory');
const { CodeGraph } = await import('../src/index');
const { LifecycleService } = await import('../src/agent-kernel/lifecycle-service');

vi.mock('../src/index', () => ({
  CodeGraph: {
    init: vi.fn(),
    open: vi.fn(),
  },
}));

vi.mock('../src/directory', () => ({
  findNearestCodeGraphRoot: vi.fn(),
  isInitialized: vi.fn(),
}));

/** 构造一个状态可编程的假 CodeGraph 实例。 */
function fakeCodeGraph(opts: {
  state?: 'indexing' | 'complete' | 'partial' | 'failed' | null;
  lastIndexedAt?: number | null;
  indexAllResult?: { success: boolean; filesIndexed: number; durationMs: number };
  syncResult?: { filesAdded: number; filesModified: number; filesRemoved: number; durationMs: number };
}) {
  return {
    getIndexState: () => opts.state ?? null,
    getLastIndexedAt: () => opts.lastIndexedAt ?? null,
    indexAll: vi.fn(async () => opts.indexAllResult ?? { success: true, filesIndexed: 0, durationMs: 0 }),
    sync: vi.fn(async () => opts.syncResult ?? { filesAdded: 0, filesModified: 0, filesRemoved: 0, durationMs: 0 }),
  } as never;
}

const mockedFind = vi.mocked(findNearestCodeGraphRoot);
const mockedIsInitialized = vi.mocked(isInitialized);
const mockedInit = vi.mocked(CodeGraph.init);
const mockedOpen = vi.mocked(CodeGraph.open);

beforeEach(() => {
  vi.resetAllMocks();
});

describe('LifecycleService.handle 参数校验', () => {
  it('非生命周期方法抛 INVALID_REQUEST', async () => {
    const svc = new LifecycleService();
    await expect(svc.handle('codegraph_explore', { workspace_path: '/ws' })).rejects.toMatchObject({
      code: 'INVALID_REQUEST',
      retryable: false,
    });
  });

  it('缺 workspace_path 抛 INVALID_REQUEST', async () => {
    const svc = new LifecycleService();
    await expect(svc.handle('codegraph_status', {})).rejects.toMatchObject({
      code: 'INVALID_REQUEST',
    });
  });
});

describe('LifecycleService.status 归一化', () => {
  it('无索引根返回 unindexed 且不打开实例', async () => {
    mockedFind.mockReturnValue(null);
    const svc = new LifecycleService();
    const payload = await svc.handle('codegraph_status', { workspace_path: '/ws/a' });
    expect(payload).toEqual({ state: 'unindexed', last_indexed_at: null });
    expect(mockedOpen).not.toHaveBeenCalled();
  });

  it('complete → ready 并带 last_indexed_at', async () => {
    mockedFind.mockReturnValue('/ws/a');
    mockedOpen.mockResolvedValue(fakeCodeGraph({ state: 'complete', lastIndexedAt: 1234 }));
    const svc = new LifecycleService();
    const payload = await svc.handle('codegraph_status', { workspace_path: '/ws/a' });
    expect(payload).toEqual({ state: 'ready', last_indexed_at: 1234 });
  });

  it('partial → ready（不阻断，由后端 sync 补齐）', async () => {
    mockedFind.mockReturnValue('/ws/a');
    mockedOpen.mockResolvedValue(fakeCodeGraph({ state: 'partial' }));
    const svc = new LifecycleService();
    const payload = await svc.handle('codegraph_status', { workspace_path: '/ws/a' });
    expect((payload as { state: string }).state).toBe('ready');
  });

  it('failed → failed', async () => {
    mockedFind.mockReturnValue('/ws/a');
    mockedOpen.mockResolvedValue(fakeCodeGraph({ state: 'failed' }));
    const svc = new LifecycleService();
    const payload = await svc.handle('codegraph_status', { workspace_path: '/ws/a' });
    expect((payload as { state: string }).state).toBe('failed');
  });
});

describe('LifecycleService.init 分流', () => {
  it('未初始化走 CodeGraph.init + indexAll', async () => {
    mockedFind.mockReturnValue(null); // 无索引根 → 用 workspacePath 自身
    mockedIsInitialized.mockReturnValue(false);
    mockedInit.mockResolvedValue(fakeCodeGraph({ indexAllResult: { success: true, filesIndexed: 10, durationMs: 5 } }));
    const svc = new LifecycleService();
    const payload = await svc.handle('codegraph_init', { workspace_path: '/ws/new' });
    expect(mockedInit).toHaveBeenCalledWith('/ws/new', { index: false });
    expect(payload).toEqual({ state: 'ready', files_indexed: 10, duration_ms: 5 });
  });

  it('已初始化目录走 open + indexAll（不触发 init 抛异常）', async () => {
    mockedFind.mockReturnValue('/ws/existing');
    mockedIsInitialized.mockReturnValue(true);
    mockedOpen.mockResolvedValue(fakeCodeGraph({ indexAllResult: { success: true, filesIndexed: 3, durationMs: 2 } }));
    const svc = new LifecycleService();
    const payload = await svc.handle('codegraph_init', { workspace_path: '/ws/existing' });
    expect(mockedInit).not.toHaveBeenCalled();
    expect(mockedOpen).toHaveBeenCalledWith('/ws/existing');
    expect(payload).toEqual({ state: 'ready', files_indexed: 3, duration_ms: 2 });
  });
});
