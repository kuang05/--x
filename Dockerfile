# 演示机依赖装不上时的兜底镜像。
#
# ⚠️ 未验证：本仓库所在环境没有 Docker，因此这个镜像**没有实际构建过**。
#    用法前请先在本机跑通：
#      docker build -t aepcb .
#      docker run --rm -p 8765:8765 aepcb
#    构建失败时优先看 requirements.lock 里的 OpenCV 包名是否与基础镜像兼容。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8

WORKDIR /app

COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --upgrade pip \
 && python -m pip install --no-cache-dir -r requirements.lock

COPY 增材电子一体化_软件/ ./软件/
COPY 增材电子一体化_数据模型/ ./数据模型/
COPY 参赛材料/ ./参赛材料/
COPY LICENSE CHANGELOG.md ./

WORKDIR /app/软件

# 自检不通过就不启动，避免“起来了但用不了”
RUN python3 -m aepcb.cli doctor || true

EXPOSE 8765
CMD ["python3", "-m", "aepcb.cli", "serve", "--port", "8765"]
