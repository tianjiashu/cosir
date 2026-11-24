"""会话管理工具：看见并联系同工作区的其他 Session（``session_list`` / ``session_read`` / ``session_send``），
以及停止自己直接派生的 Session（``session_stop``）。

寻址规则在 ``addressing``（发送与读取认整个工作区、停止只认直接一层），进度投影在 ``progress``。
派生本身（委派、分叉）、派生汇报与 Agent 消息体的构造仍在 ``core.tool.multi_agent``。
"""
