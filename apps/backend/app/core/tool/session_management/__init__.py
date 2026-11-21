"""会话管理工具：给其他 Session 发消息（``session_send``），以及管理自己派生树里的 Session
（``session_list`` / ``session_read`` / ``session_stop``）。

寻址规则在 ``addressing``（发送认整个工作区、读取认整棵树、停止只认直接一层），进度投影在 ``progress``。
派生本身（委派、分叉）、派生汇报与 Agent 消息体的构造仍在 ``core.tool.multi_agent``。
"""
