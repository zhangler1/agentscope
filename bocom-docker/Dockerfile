# bocom-starter 服务镜像：启动程序 + bocom-as 发行版 whl + agentscope SDK whl
# 构建上下文 = 交付物根（含 bocom-as / bocom-docker / bocom-starter / examples），
# 即 bocom-docker 的父目录（见 docker-compose.yml agentscope.build.context）。
# 不依赖 agentscope-sdk 根仓库——SDK 与 bocom-as 均以 whl 形式随
# bocom-as/wheels 分发，依赖链：starter → bocom_as whl → agentscope whl。
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

# 第一步：复制 bocom-starter 依赖清单与 bocom-as/wheels 双 whl，安装全量
# 依赖链（starter 为应用型项目，package=false，只装依赖）。
# [tool.uv.sources] 相对路径 ../bocom-as/wheels 与下方 COPY 目标一致，
# 两个 whl 均离线本地解析；只要 pyproject.toml 与 wheels/ 不变，这层被缓存。
COPY bocom-starter/pyproject.toml /app/bocom-starter/pyproject.toml
COPY bocom-as/wheels /app/bocom-as/wheels
RUN uv sync --project /app/bocom-starter

# 第二步：复制 bocom-starter 启动程序（config / providers / agentscope
# 均来自第一步安装的 whl，不再 editable 安装 bocom-as 源码）。
# 运行时经 volume 挂载覆盖为宿主源码（compose：../bocom-starter/main.py:/app/main.py:ro）。
COPY bocom-starter/main.py /app/main.py

WORKDIR /app

EXPOSE 8000

# 启动服务（main.py 内 uvicorn.run("main:app", ...)）
CMD ["python", "main.py"]
