FROM node:20-bookworm

WORKDIR /app

# 复制 package 文件安装依赖
COPY package.json ./
RUN npm install

# 复制源码（运行时通过 volume 覆盖）
COPY . .

# 构建 TypeScript
RUN npm run build

EXPOSE 3000

# 启动服务
CMD ["npm", "start"]
