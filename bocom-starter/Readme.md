# bocom-starter — 行内模型平台参考启动程序

基于 agentscope 官方示例的启动程序，**额外装配行内模型平台**（bocom-as 发行版：
`config` / `providers`），完整覆盖 4 条主流程：**行内模型凭证、选择模型、
add_think、api_key 刷新**。

## 安装

bocom-starter 是应用型项目，依赖链全部离线解析自 `bocom-as/wheels/`
内的双 whl（`starter → bocom_as whl → agentscope whl`），不查 PyPI：

```bash
cd bocom-starter
uv sync
```

> 手动安装（pip）：先 `pip install ../bocom-as/wheels/*.whl`（两个 whl，
> 顺序任意），再按需补装其余依赖；推荐直接用 uv sync。

## 启动

### 本地直跑

```bash
cd bocom-starter
# 按需编辑 .env（所有项均有默认值，可省略）
uv run uvicorn main:app --reload
```

### Docker 部署（bocom-docker）

```bash
cd bocom-docker
docker compose up -d
```

- **镜像**：复用 `agentscope-service:oss`（不构建）；bocom-starter/main.py
  由 volume 挂载（dev 可热改，uvicorn 自动 reload）；SDK（agentscope）与
  bocom-as（config/providers）均以 whl 固化在镜像 `.venv` 内——平台变更
  需重建 whl 后 `--build` 重建镜像；
- **环境变量**：经 `env_file` 复用 `bocom-starter/.env`（`localhost` 类地址
  由 compose `environment` 覆盖为容器网络服务名 `redis`）；修改 `.env`
  后需 `docker compose up -d` 重建容器生效；
- **端口**：`8000` bocom-starter API，`80` nginx webui 网关（`/api/*`
  剥前缀转发），`6379` redis；宿主端口均可参数化覆盖
  （`AGENTSCOPE_HOST_PORT` / `REDIS_HOST_PORT` / `NGINX_HOST_PORT` /
  `WEBUI_BACKEND_HOST_PORT`，默认 8000 / 6379 / 80 / 3000）；
- **固化镜像（备选）**：若不想依赖 `agentscope-service:oss`，可用
  `docker compose up -d --build`（bocom-docker/Dockerfile 将定制 SDK 与
  bocom-as 装进镜像，依赖版本与 SDK 匹配）。

启动后 API 文档见 <http://localhost:8000/docs>。

### 多实例（端口 + 数据隔离）

宿主端口由环境变量覆盖，`-p` 指定 project 名（容器名 / 网络 / volume
全部带 project 前缀，数据完全隔离）：

```bash
# 实例 2（实例 1 用默认端口）
AGENTSCOPE_HOST_PORT=8010 REDIS_HOST_PORT=6380 NGINX_HOST_PORT=81 \
WEBUI_BACKEND_HOST_PORT=3001 docker compose -p bocom2 up -d
```

- **容器内逻辑零改动**：`ELLM_REDIS_HOST=redis` 在各自 project 网络内解析，
  `.env` 不用动；
- 端口变量可写 `bocom-docker/.env`（仅 compose 变量替换、不进入容器，
  与 `bocom-starter/.env` 互不干扰）；
- `NGINX_HOST_PORT=0` 跳过 webui 网关的宿主端口映射；
- 查看/日志：`docker compose -p bocom2 ps`、
  `docker compose -p bocom2 logs -f agentscope`。

## 打包交付

最终交付物只包含 4 个包（**agentscope-sdk 根仓库不进交付物**）：

```
bocom-as/        # 发行版：config / providers / wheels（双 whl：SDK + bocom-as）
bocom-docker/    # 部署：docker-compose.yml + Dockerfile + nginx/
bocom-starter/   # 智能体开发脚手架：main.py + pyproject.toml + .env + Readme.md
examples/        # webui 全家桶（webui-backend / webui-frontend）
```

依赖链（全部离线，不查 PyPI）：

```
bocom-starter (pyproject.toml, 应用型项目)
  └─ bocom_as-<v>.whl          ← bocom-as/wheels/（config / providers / _models）
       └─ agentscope-<v>.whl   ← bocom-as/wheels/（SDK 核心 + extras 元数据）
```

- **bocom-as whl 依赖**：[pyproject.toml](../bocom-as/pyproject.toml) 打包
  `config` + `providers`；agentscope SDK 以 whl 依赖引入（版本固定
  `==2.0.7.post1`，经 `[tool.uv.sources]` 指向 wheels/ 本地解析），extras
  为 full 按行内基座裁剪：去 models（行内 ELLM 大模型）、memory（行内
  统一记忆）、rag 与 vdb 全部（行内统一知识库）、channel（无外部 IM）、
  storage-s3（随知识库移除）；另直依赖 aiomysql（OB MySQL 模式驱动，
  storage-sql extra 不含驱动）；
- **bocom-starter whl 依赖**：[pyproject.toml](pyproject.toml) 仅直接依赖
  `bocom-as` whl；agentscope whl 路径经 `[tool.uv.sources]` 声明供传递
  解析（`starter → bocom_as whl → agentscope whl`）；
- **发行版打包（SDK 或 bocom-as 变更后）**：

  ```bash
  # 1. SDK whl：根仓库构建后拷入（删旧 whl）
  uv build && cp dist/agentscope-<版本>-py3-none-any.whl bocom-as/wheels/
  # 2. bocom-as whl：直接输出到 wheels/（--out-dir）
  uv build --wheel --project bocom-as --out-dir bocom-as/wheels
  # 3. 同步更新两处 pyproject.toml 的版本号与 [tool.uv.sources] whl 文件名
  #    （bocom-as 与 bocom-starter），并删除 wheels/ 下旧 whl
  # 4. 重建镜像：docker compose up -d --build
  ```

  > `bocom-as/wheels/.gitignore` 是防止 uv build 重新生成忽略规则的占位
  > 文件（uv 仅在缺失时生成），**勿删**；两个 whl 均需入库跟踪。

- **两种部署模式**：
  - 快速（oss 镜像）：`docker compose up -d`——复用 `agentscope-service:oss`，
    bocom-starter/main.py volume 挂载（可热改），SDK 与 bocom-as 用镜像内
    whl 版本；
  - 固化（build 模式）：`docker compose up -d --build`——以交付物根
    （bocom-docker 父目录）为构建上下文，依赖与 bocom-as 发行版绑定；
    构建覆盖同 tag 镜像，需保留 oss 先
    `docker tag agentscope-service:oss agentscope-service:oss.bak`。

## 行内模型平台 4 条主流程

### ① 行内模型凭证

`POST /credential` 创建凭证（`type="bocom_ellm_credential"`，必填
`api_key` / `base_url` / `model`，可选 `organization` / `scene_code` /
`api_key_url` / `inject_think_tag` / `apikey_expires_at`）。

请求体为 `{"data": {...}}`——`type` 等字段全部位于 `data` 内
（`CredentialFactory.from_dict` 按 `type` discriminator 反序列化）：

```json
{
  "data": {
    "type": "bocom_ellm_credential",
    "name": "行内大模型",
    "api_key": "sk-xxx",
    "base_url": "http://ellm-gateway.example/v1",
    "model": "deepseek-v4-flash",
    "scene_code": "P2024146",
    "api_key_url": "http://ellm.example/ELLM-OMSERVICE/createSceneApiKey.do"
  }
}
```

- `GET /credential/schemas` 确认 `bocom_ellm_credential` 已注册（导入
  `providers.credential` 即自动注册，幂等）；
- `GET /model/credential?credential_id=...` 按凭证查候选模型（含凭证绑定
  单模型过滤）；
- `PATCH /model/credential/{credential_id}` 部分更新（仅覆盖传入字段；
  api_key 刷新也写回同一凭证记录）。

### ② 选择模型

- `GET /ellm-models` 查模型候选列表；`POST` / `PUT` / `DELETE
  /ellm-models` 管理候选（Redis 模型表，field=模型名、value=JSON
  `{think_tag, context_size, output_size}`；Redis 不可用时降级
  `providers/_models/*.yaml`）；
- 会话配置 `chat_model_config.model` 填模型名，`/chat` 调用时
  `EllmChatModel` 按模型名读取 think_tag / context_size。

### ③ add_think（会话级覆盖）

`PUT /ellm-models/session/{session_id}/think-tag` 设置会话级覆盖
（body `{"think_tag": true|false}`，写 Redis，TTL 4h），`DELETE` 清除、
`GET` 查询（无覆盖返回 `{"think_tag": null}`）。

生效优先级：**会话级覆盖 > Redis 模型表 > 默认 False**。

```bash
curl -X PUT http://localhost:8000/ellm-models/session/sess-1/think-tag \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' -d '{"think_tag": true}'
```

### ④ api_key 刷新（自动，无需人工干预）

- 每次模型调用前中间件惰性检查：`apikey_expires_at` 过期（含
  `ELLM_KEY_REFRESH_AHEAD_SECS` 提前窗口）→ `MessageBus.acquire_lock`
  防抖 → 同步调 `fetch_ellm_key(api_key_url, scene_code)` 取新 key →
  写回凭证记录 → `set_api_key` 注入请求头；
- 401 `invalid_api_key` 时强制刷新并重试当前调用一次；刷新失败则标记
  凭证过期，下一次调用走惰性刷新恢复；
- 日志关键字：`injected refreshed ELLM key`。

## 快速上手：添加行内凭证并使用行内模型 chat

以下示例默认服务运行于 `http://localhost:8000`（Docker 部署见
bocom-docker，宿主端口 8000；容器内访问宿主机服务用
`host.docker.internal`）。

> 所有业务接口（含行内模型平台接口）都要求 `X-User-ID` 请求头
> （临时 header 身份，缺失返回 422 `Field required`），示例统一用
> `test-user`。

### 1. 创建行内凭证

```bash
curl -X POST http://localhost:8000/credential/ \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' \
  -d '{
    "data": {
      "type": "bocom_ellm_credential",
      "name": "行内ELLM",
      "api_key": "sk-xxx",
      "base_url": "http://host.docker.internal:8001/v1",
      "model": "deepseek-v4-flash",
      "scene_code": "P2024146",
      "api_key_url": "http://ellm.example/ELLM-OMSERVICE/createSceneApiKey.do",
      "inject_think_tag": true
    }
  }'
# → {"credential_id": "cred-xxx"}
```

要点：

- `api_key` 为 `SecretStr`，传普通字符串即可；`base_url` 为 OpenAI 兼容
  端点（**以 `/v1` 结尾**）；
- `scene_code` / `api_key_url` 用于 api_key 自动刷新（见主流程④），
  不填不影响 chat；
- `GET /credential/` 查列表，`GET /credential/schemas` 确认注册。

### 2. 创建 agent（无预置，需手动建一次）

```bash
curl -X POST http://localhost:8000/agent/ \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' \
  -d '{"name": "ellm-assistant"}'
# → {"agent_id": "agent-xxx"}
```

### 3. 创建会话并绑定行内模型

```bash
curl -X POST http://localhost:8000/sessions/ \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' \
  -d '{
    "agent_id": "agent-xxx",
    "chat_model_config": {
      "type": "bocom_ellm_credential",
      "credential_id": "cred-xxx",
      "model": "deepseek-v4-flash",
      "parameters": {}
    }
  }'
# → {"session_id": "sess-xxx"}
```

要点：

- `model` 填 `GET /ellm-models` 候选中的模型名（Redis 模型表，不可用时
  降级 `providers/_models/*.yaml`）；
- 会话也可先建后补模型：`PATCH /sessions/{session_id}` 更新
  `chat_model_config`。

### 4. 使用行内模型 chat（SSE 流式）

```bash
curl -N -X POST http://localhost:8000/chat/ \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' \
  -d '{
    "agent_id": "agent-xxx",
    "session_id": "sess-xxx",
    "input": {
      "name": "user",
      "role": "user",
      "content": [{"type": "text", "text": "你好，介绍一下你自己"}]
    }
  }'
```

- 响应为 SSE 流（`curl -N` 实时输出）；
- 每次调用前中间件自动检查/刷新 api_key（惰性预刷 + 401 强制刷新重试），
  无需人工干预。

### 5. 会话级 think-tag 覆盖（可选）

```bash
# 开启覆盖（优先于 Redis 模型表与凭证开关，TTL 4h）
curl -X PUT http://localhost:8000/ellm-models/session/sess-xxx/think-tag \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' -d '{"think_tag": true}'

# 查询 / 清除
curl -H 'x-user-id: test-user' http://localhost:8000/ellm-models/session/sess-xxx/think-tag
curl -X DELETE http://localhost:8000/ellm-models/session/sess-xxx/think-tag \
  -H 'x-user-id: test-user'
```

开启后同会话流式响应的首个文本段出现 `<think>` 前缀。

## 本地测试（无行内网络）

行内凭证（`base_url` / `api_key` / `api_key_url`）指向行内服务，只在行内
网络生效。本地验证用随仓库提供的 **Mock ELLM 网关**
（[dev_ellm_gateway.py](dev_ellm_gateway.py)）：宿主机起一个假的 OpenAI
兼容网关，凭证指向它即可走通全链路（含 api_key 刷新中间件）。

```bash
# 1. 启动 mock 网关（0.0.0.0:8001）
python dev_ellm_gateway.py
```

### 测试凭证（两种配置）

**a. 只测 chat**——`apikey_expires_at` 设未来值，跳过刷新链路，
`api_key` 任意：

```bash
curl -X POST http://localhost:8000/credential/ \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' \
  -d '{
    "data": {
      "type": "bocom_ellm_credential",
      "name": "本地Mock",
      "api_key": "test-local-key",
      "base_url": "http://localhost:8001/v1",
      "model": "deepseek-v4-flash",
      "inject_think_tag": true,
      "apikey_expires_at": 4102444800
    }
  }'
```

**b. 连刷新链路一起测**——`api_key_url` 指向 mock 的 `/v1/keys`，
不设 `apikey_expires_at`（视为已过期，每次调用触发取 key）：

```bash
curl -X POST http://localhost:8000/credential/ \
  -H 'Content-Type: application/json' \
  -H 'x-user-id: test-user' \
  -d '{
    "data": {
      "type": "bocom_ellm_credential",
      "name": "本地Mock+刷新",
      "api_key": "test-local-key",
      "base_url": "http://localhost:8001/v1",
      "model": "deepseek-v4-flash",
      "scene_code": "P2024146",
      "api_key_url": "http://localhost:8001/v1/keys",
      "inject_think_tag": true
    }
  }'
```

> 用配置 a/b 创建的凭证 id，后续按「快速上手」的 2-5 步走 agent → 会话 →
> chat 即可；服务端日志可见 `fetch_ellm_key: key fetched`（配置 b）与
> `injected refreshed ELLM key`。

### Docker 部署时的地址

容器内访问宿主机上的 mock 网关：`base_url=http://host.docker.internal:8001/v1`
（Linux 主机需在 bocom-docker compose 的 agentscope 服务加
`extra_hosts: ["host.docker.internal:host-gateway"]`）。

## 环境变量（见 .env 示例）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `mysql+aiomysql://root:@localhost:2881/agentscope?charset=utf8mb4` | 应用主存储 OceanBase（MySQL 模式，aiomysql 驱动） |
| `ELLM_REDIS_HOST` / `ELLM_REDIS_PORT` | `localhost` / `6379` | 缓存 Redis（行内模型平台模型表，可独立指定） |
| `ELLM_REDIS_TIMEOUT` | `1.0` | Redis 连接超时（秒） |
| `ELLM_REDIS_MAX_CONNECTIONS` | `200` | Redis 连接池上限 |
| `ELLM_MODEL_THINK_TAG_KEY` | `bocomadp:model:think_tag` | 模型 think-tag 表 key（与 bocomadp 数据兼容，可覆盖隔离） |
| `ELLM_KEY_REFRESH_AHEAD_SECS` | `120.0` | api_key 提前刷新窗口（秒） |

> bocom-as 的 `config.get_ellm_settings()` 每次调用重建、环境变量热读；
> `.env` 由宿主应用加载（本仓库提供示例），config 不主动加载。
