"""Task inbox 的消息语义：调度规则（``dispatch_policy``）与单次执行的投递通道（``run_mailbox``）。

外部队列的持久化与调度 actor 仍在 ``service/conversation_run``；这里只放不依赖后端的纯规则与通道。
"""
