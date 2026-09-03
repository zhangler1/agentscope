# bocom-starter 服务镜像（自包含）
# 交付物 = 本目录（bocom-starter/）：Dockerfile + main.py + .env + wheels/ + Readme.md
# SDK（agentscope）与行内模型平台扩展（bocom_as whl）均以包形式安装，不拷贝源码。
# 构建（上下文 = 本目录）：
#   docker build -t agentscope-service:bocom \
#     --build-arg PIP_INDEX_URL=https://<行内pip源>/simple .
# 行内无外网，必须注入行内 pip 源；缺省回退官方 PyPI（仅本地可直连外网时用）。
FROM python:3.11-bookworm

# 系统依赖（curl: uv 安装器；ripgrep: LocalWorkspace 的 Grep 工具）
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates ripgrep \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv
RUN curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh

WORKDIR /app

# 虚拟环境（uv pip 定位到它；PATH 前置保证 python/uvicorn 取自 .venv）
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# 行内 pip 源（uv 兼容标准 PIP_INDEX_URL）
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=$PIP_INDEX_URL

# 依赖层 1（缓存层，体积大头）：agentscope SDK 及其全量依赖。
# 版本须与 bocom_as whl 声明的约束一致；SDK 升级时同步此 ARG。
ARG AGENTSCOPE_VERSION=2.0.7
RUN uv venv /app/.venv \
    && uv pip install "agentscope==${AGENTSCOPE_VERSION}"

# 依赖层 2：行内模型平台扩展（config + providers，本地 whl）。
# wheels/ 变更（新版本 whl）时本层重跑；依赖已被层 1 覆盖的部分命中 uv 缓存。
COPY wheels/ /app/wheels/
RUN uv pip install /app/wheels/bocom_as-0.1.0-py3-none-any.whl

# 启动程序层：main.py（.env 由 compose env_file 注入，不进镜像）
COPY main.py /app/main.py

EXPOSE 8000
# main.py 内 uvicorn.run("main:app", ...)：生产默认不 reload，
# 本地开发可设 UVICORN_RELOAD=true
CMD ["python", "main.py"]
