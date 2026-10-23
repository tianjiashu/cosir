"""Task runtime 的进程内广播能力。

本包集中承载配置变更到已运行 Agent 的运行期通知（``NoticeBody``）广播器：通知写入目标 Task
的外部消息队列，不负责配置持久化、HTTP 协议映射或 Agent 执行生命周期管理。
"""
