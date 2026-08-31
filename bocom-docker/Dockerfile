# bocom-starter 服务镜像：bocom-as 自包含发行版（SDK + 行内模型平台）+ 启动程序
# 构建上下文 = 交付物根（含 bocom-as / bocom-docker / bocom-starter / examples），
# 即 bocom-docker 的父目录（见 docker-compose.yml agentscope.build.context）。
# 不依赖 agentscope-sdk 根仓库——SDK 源码随 bocom-as/src 分发。
FROM python:3.11-bookworm

# 安装系统依赖（curl: uv 安装器；ripgrep: LocalWorkspace 的 Grep 工具）
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates ripgrep \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv
RUN curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh

WORKDIR /app

# 虚拟环境（与 oss 镜像同构：/app/.venv；uv pip 也定位到它）
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# 第一步：只复制 bocom-as 的 pyproject.toml，安装全量依赖（不含项目本身）。
# 依赖列表随 bocom-as 发行版维护（SDK 依赖 + service / storage-redis /
# workspace-docker 已并入主列表）；只要 pyproject.toml 不变，这层被 Docker 缓存。
COPY bocom-as/pyproject.toml /app/bocom-as/pyproject.toml
RUN uv sync --project /app/bocom-as --no-install-project

# 第二步：复制 bocom-as 完整源码（config / providers / src=agentscope SDK），
# 以可编辑模式安装（editable 注册 agentscope / config / providers 全部包）。
# 运行时源码经 volume 挂载（compose：../bocom-as:/app/bocom-as:ro），修改即时生效。
COPY bocom-as /app/bocom-as
RUN uv pip install --no-deps -e /app/bocom-as

# 第三步：复制 bocom-starter 启动程序。
# 运行时经 volume 挂载覆盖为宿主源码（compose：../bocom-starter/main.py:/app/main.py:ro）。
COPY bocom-starter/main.py /app/main.py

WORKDIR /app

EXPOSE 8000

# 启动服务（main.py 内 uvicorn.run("main:app", ...)）
CMD ["python", "main.py"]
