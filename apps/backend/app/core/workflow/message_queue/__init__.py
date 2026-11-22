"""Session 消息队列：消息语义（``SessionMessage`` 与 ``message_bodies``）、外部队列 ``SessionMessageQueue``
与待处理消息的挑选规则 ``PendingMessages``；推进判定在 ``core/workflow/dispatch_policy``。

单次驱动的投递通道 ``RunMailbox`` 在 ``execution_state``，调度 actor 在 ``core/session_actor.py``。
"""
