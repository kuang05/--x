"""增材电子一体化软件 · 核心运行时（纯 Python + numpy，无外部服务依赖）。

模块划分对应需求四域：
    store     持久层（复用 58 表 schema）
    step/board  导入（STEP 子集、板文件）
    routing   布线、连通性
    mrc       可打性规则与评分
    slicing   三层七道次切片
    gcode     确定性导出与双哈希
    printer   虚拟打印、故障、缺陷
    agent     计划—执行—复核—重放
    accept    T01–T20 验收运行器
    report    HTML/SVG 报告
"""

__version__ = "1.0.0"
