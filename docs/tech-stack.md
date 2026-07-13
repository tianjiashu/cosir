# 技术栈与运行形态

本文记录 `coding-agent` 的技术栈、运行形态和当前不提前定死的技术决策。

## 桌面客户端

- 桌面壳：Tauri 2
- 前端框架：React
- 前端语言：TypeScript
- 构建工具：Vite
- UI 组件：shadcn/ui + Radix UI + Tailwind CSS + lucide-react
- 高密度数据视图：TanStack Table / TanStack Virtual
- 代码与 diff 视图：Monaco Editor；如后续确认体积或性能压力过高，再评估 CodeMirror 6
- 命令面板：cmdk
- UI 定位：客户端优先，承载会话、项目视图、执行状态、权限确认、变更展示、日志入口。

## 后端与 Agent Runtime

- 后端语言：Python
- API 框架：FastAPI
- Agent 编排：LangGraph
- LangGraph 安全基线：后端依赖只在 Python 3.10+ 安装 `langgraph==1.0.10`；当前 Python 3.9 本地开发使用工作流 fallback，不启用旧版 LangGraph，避免把后续 checkpoint 能力建立在有已知风险的旧基线上。
- 模型接入：第一阶段优先支持 OpenAI 协议，优先适配 DeepSeek，后续扩展其他大模型。
- 前后端通信：第一版以 HTTP API + SSE 为主；WebSocket 仅作为后续双向实时场景预留。
- 模型调用：第一版使用模型供应商的 streaming 输出；后端 Model Adapter 将供应商 stream 规范化为内部增量事件，Runtime 再把运行事件通过 SSE 推送给客户端。
- 运行形态：桌面客户端启动本地 Python 后端 sidecar 进程。

## 存储与日志

- 本地数据库：SQLite
- 日志：Python logging / structlog
- 日志文件：必须固定写入可排查日志文件，例如 `logs/app.log`
- 持久化对象：会话、任务、运行事件、用户规则、工具配置、上下文索引、checkpoint 元数据。

## 本地一体化运行形态

这是一个本地一体化桌面应用，不是前端连接远程服务器后端的 Web 产品。

```text
一个桌面 App
  ├─ Tauri + React 前端
  └─ 本地 Python FastAPI + LangGraph 后端
```

- Tauri 桌面客户端负责窗口、交互、会话展示、权限确认、项目视图、变更展示。
- Tauri 桌面客户端负责启动、探活、停止 Python 后端进程。
- Python 后端负责 FastAPI 服务、SSE 事件流、LangGraph 执行、工具系统、上下文管理、SQLite、日志文件。
- Python 后端只监听本机地址，不作为服务器部署。
- UI 与后端通过本地 HTTP API + SSE 通信。
- 后端启动时应生成本次会话访问凭据，避免本机其他进程随意调用。
- 第一阶段 Agent 任务可在后端进程内以任务为单位运行；后续如需要更强隔离，再演化为独立 worker / subprocess / sandbox。

## 暂不提前定死

- 向量数据库
- DeepSeek 之后的大模型接入顺序
- 后端最终打包 Python 版本；若第一阶段要启用 LangGraph 安全基线，应升级到 Python 3.10+
- 具体密钥管理方式
- 插件协议细节
- 完整打包与自动更新方案
