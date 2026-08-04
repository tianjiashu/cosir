/**
 * agent-kernel tool-service 单元测试（对齐设计文档 T10）。
 *
 * 验证 ToolService.dispatch 的核心契约：
 * - `workspace_path` 被映射为上游 `ToolHandler.execute` 的 `args.projectPath`；
 * - `workspace_path` 本身不作为参数透传（避免上游收到不认识的字段）；
 * - 查询方法名原样转发给 ToolHandler.execute；
 * - 上游 ToolResult 被归一化为 QueryOutcome（content / isError）。
 *
 * 用轻量 mock 替代真实 MCPEngine/ToolHandler，避免依赖真实索引与 node 运行时。
 */

import { describe, it, expect, vi } from 'vitest';
import { ToolService } from '../src/agent-kernel/tool-service';
import type { WorkspaceService } from '../src/agent-kernel/workspace-service';
import type { ToolHandler } from '../src/mcp/tools';

function makeToolService(
  handler: ToolHandler,
  opts: { ready?: boolean; ensureInitialized?: () => Promise<void> } = {},
): ToolService {
  const fakeWs = {
    getToolHandler: () => handler,
    getStatus: () => ({ ready: opts.ready ?? true, projectPath: null, closed: false }),
    ensureInitialized: opts.ensureInitialized ?? vi.fn(async () => {}),
  } as unknown as WorkspaceService;
  return new ToolService(fakeWs);
}

describe('ToolService.dispatch workspace_path -> projectPath mapping', () => {
  it('maps workspace_path to args.projectPath and strips it from top-level args', async () => {
    const recorded: { name: string; args: Record<string, unknown> } = { name: '', args: {} };
    const handler = {
      execute: vi.fn(async (name: string, args: Record<string, unknown>) => {
        recorded.name = name;
        recorded.args = args;
        return { content: [{ type: 'text', text: 'ok' }], isError: false };
      }),
    } as unknown as ToolHandler;

    const service = makeToolService(handler);
    const outcome = await service.dispatch('codegraph_explore', {
      workspace_path: '/abs/path/to/ws',
      query: 'loginUser',
    });

    expect(recorded.name).toBe('codegraph_explore');
    expect(recorded.args['projectPath']).toBe('/abs/path/to/ws');
    expect(recorded.args['workspace_path']).toBeUndefined();
    expect(recorded.args['query']).toBe('loginUser');
    expect(outcome.content).toEqual([{ type: 'text', text: 'ok' }]);
    expect(outcome.isError).toBe(false);
  });

  it('routes a second distinct workspace_path through projectCache (different projectPath)', async () => {
    const calls: Array<Record<string, unknown>> = [];
    const handler = {
      execute: vi.fn(async (_name: string, args: Record<string, unknown>) => {
        calls.push(args);
        return { content: [{ type: 'text', text: 'x' }], isError: false };
      }),
    } as unknown as ToolHandler;

    const service = makeToolService(handler);
    await service.dispatch('codegraph_explore', { workspace_path: '/ws1', query: 'a' });
    await service.dispatch('codegraph_explore', { workspace_path: '/ws2', query: 'b' });

    expect(calls[0]['projectPath']).toBe('/ws1');
    expect(calls[1]['projectPath']).toBe('/ws2');
    // 两次调用带不同 projectPath，证明跨 workspace 路由驱动 projectCache 懒加载。
    expect(calls[0]['projectPath']).not.toBe(calls[1]['projectPath']);
  });

  it('propagates upstream isError as outcome.isError without throwing', async () => {
    const handler = {
      execute: vi.fn(async () => ({ content: [{ type: 'text', text: 'nope' }], isError: true })),
    } as unknown as ToolHandler;

    const service = makeToolService(handler);
    const outcome = await service.dispatch('codegraph_search', { workspace_path: '/ws' });
    expect(outcome.isError).toBe(true);
  });

  it('rejects when workspace_path is missing', async () => {
    const handler = { execute: vi.fn(async () => ({ content: [], isError: false })) } as unknown as ToolHandler;
    const service = makeToolService(handler);

    await expect(service.dispatch('codegraph_explore', { query: 'x' })).rejects.toThrow(
      /workspace_path/,
    );
  });

  it('rejects non-query methods', async () => {
    const handler = { execute: vi.fn(async () => ({ content: [], isError: false })) } as unknown as ToolHandler;
    const service = makeToolService(handler);

    await expect(
      service.dispatch('kernel.hello' as never, { workspace_path: '/ws' }),
    ).rejects.toThrow(/not a query tool/);
  });

  it('calls ensureInitialized when engine is not ready (retry-on-next-call contract)', async () => {
    const handler = {
      execute: vi.fn(async (_name: string, args: Record<string, unknown>) => ({
        content: [{ type: 'text', text: 'ok' }],
        isError: false,
      })),
    } as unknown as ToolHandler;
    const ensureInitialized = vi.fn(async () => {});
    // getStatus().ready 初始为 false（default project 未打开）。
    const service = makeToolService(handler, { ready: false, ensureInitialized });

    await service.dispatch('codegraph_explore', { workspace_path: '/ws', query: 'a' });

    // 未就绪时必须触发幂等重试；引擎构造即创建 ToolHandler，故不能靠 handler
    // 是否为 null 判断（那会永远跳过重试）。这正是修复项1 的回归点。
    expect(ensureInitialized).toHaveBeenCalledTimes(1);
    expect(ensureInitialized).toHaveBeenCalledWith('/ws');
  });

  it('throws INVALID_REQUEST when workspace_path is missing via KernelDispatchError contract', async () => {
    const handler = { execute: vi.fn(async () => ({ content: [], isError: false })) } as unknown as ToolHandler;
    const service = makeToolService(handler, { ready: false });

    // 缺 workspace_path 须抛携带 INVALID_REQUEST 的错误，而非裸 Error；
    // server 据此精确映射响应码，不依赖 message 子串猜测。
    await expect(service.dispatch('codegraph_explore', {})).rejects.toMatchObject({
      code: 'INVALID_REQUEST',
    });
  });
});
