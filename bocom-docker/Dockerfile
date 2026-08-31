# bocom-starter 服务镜像：行内定制 agentscope SDK + bocom-as 发行版 + 启动程序
# 构建上下文为 agentscope-sdk 仓库根（见 bocom-docker/docker-compose.yml build 配置）
FROM python:3.11-bookworm

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates ripgrep \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv
RUN curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh

WORKDIR /app

# 创建虚拟环境
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# 第一步：只复制依赖文件，安装所有依赖（不含项目本身）
# 这样只要 pyproject.toml 不变，这层就会被 Docker 缓存
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv sync --extra service --extra storage-redis --extra workspace-docker --no-install-project

# 第二步：复制完整源码并以可编辑模式安装行内定制 agentscope SDK
# 源码通过 volume 挂载（compose：../src:/app/src:ro），修改即时生效
RUN uv pip install --no-deps -e .

# 第三步：安装 bocom-as 发行版（config / providers 顶层子包）
# 运行时源码经 volume 挂载（compose：../bocom-as:/app/bocom-as:ro）
COPY bocom-as/pyproject.toml bocom-as/
COPY bocom-as/config bocom-as/config
COPY bocom-as/providers bocom-as/providers
RUN uv pip install --no-deps -e bocom-as

# 第四步：复制 bocom-starter 启动程序
# 运行时经 volume 挂载覆盖为宿主源码（compose：../bocom-starter:/app/bocom-starter）
COPY bocom-starter/ bocom-starter/

WORKDIR /app/bocom-starter

EXPOSE 8000

# 启动服务（main.py 内 uvicorn.run("main:app", ...)）
CMD ["python", "main.py"]
