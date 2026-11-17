"""内置浏览器的后端侧客户端：标签、下载与页面都归宿主，这里只管后端怎么调宿主的 ``browser.*`` 方法。

- ``browser_call``：浏览器工具调宿主的入口（注入归属 Session、标签上下文与交接约束）；
- ``deadlines``：宿主方法时限的镜像与后端等待时限；
- ``tab_context``：宿主管理标签要用、只有后端知道的事实（Agent 与用户两条路径共用）；
- ``tab_reclaim``：Session 删除后的回收通知与链路连上时的对账。

不依赖工具 handler（``tests/test_browser_runtime_import_boundary.py`` 守卫）：工具与应用层调它，反过来不行。
"""
