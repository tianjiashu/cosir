"""LangGraph 持久化运行相关模块。

包含 checkpointer 工厂等，用于把 Workflow 编排层的状态机交给 LangGraph 的
``SqliteSaver`` 真实落盘，从而支持断点续跑、interrupt/resume 与状态查询。
"""
