"""Small, framework-independent Agent runtime.

包结构（依赖方向自上而下，不出现环）：

- ``config``      配置加载与默认值
- ``contracts``   各层之间的数据契约
- ``model``       模型协议解析与 HTTP 调用
- ``context``     上下文组装与预算裁剪
- ``tools``       工具注册表与内置工具
- ``storage``     持久化（会话/运行/消息/轨迹/待办/资源）
- ``runtime``     四步执行循环，把上面这些串起来
- ``api``         FastAPI 接口层与静态前端托管
"""
