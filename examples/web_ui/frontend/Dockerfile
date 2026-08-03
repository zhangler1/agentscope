# 构建阶段：Debian 基础
FROM node:20-bookworm AS builder

RUN corepack enable && corepack prepare pnpm@latest --activate

WORKDIR /app

# 复制 package 文件安装依赖
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

# 复制源码并构建
COPY . .
RUN pnpm build

# 运行阶段：nginx:alpine 托管静态文件
FROM nginx:alpine

# 复制构建产物
COPY --from=builder /app/dist /usr/share/nginx/html

# 复制自定义 nginx 配置（可选）
# COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
