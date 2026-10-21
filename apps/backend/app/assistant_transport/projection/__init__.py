"""执行事件到 Transport snapshot 的投影，以及实时事件与持久消息重建共用的纯展示转换。

事件只承载事实；每类事件的 mutation 规划在本包按类型实现，``event_projection.plan_event`` 是
唯一分派入口。
"""
