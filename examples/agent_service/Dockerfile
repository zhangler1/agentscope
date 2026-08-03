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

# 第二步：复制完整源码并以可编辑模式安装项目本身
# 源码通过 volume 挂载，修改即时生效
RUN uv pip install --no-deps -e .

# 复制启动脚本
COPY examples/agent_service/main.py ./

EXPOSE 8000

# 启动服务
CMD ["python", "main.py"]
