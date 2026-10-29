# learn-fastapi（AI / LLM 服务骨架）

FastAPI + LangChain 的分层服务骨架。分层跟随 FastAPI 生态的惯例：路由薄、业务进 service、SQL 只落在仓储；读代码时沿着"请求从哪进来 → 业务在哪 → 数据从哪出去"这条线走，比按目录逐个看更快。

对外链路四条：**邮箱注册登录**（`/api/v1/auth`）、**LLM 对话**（`/api/v1/chat`）、**会话与消息**（`/api/v1/conversations`、`/api/v1/messages`）、**健康检查**（`/api/v1/health`）。底层铺好了雪花 ID、MySQL + Alembic 迁移、Redis、统一响应外壳、业务错误码、全局异常处理。

对话是**有状态且必须登录**的：服务端建会话、落两条消息、按 `conversation_id` 加载上下文（请求体里没有 `history`，客户端无法指定喂给模型的上下文）；流式回答在 `done` 之前就提交；生成与连接解耦（生产者任务 + Redis Stream 分片缓冲），断线后拿 `Last-Event-ID` 调 `GET /chat/stream/{message_id}` 就能接着读，不重新生成、不重复计费，长时间没人回来才停（`interrupted`，保留已生成部分）。每条助手回复可以赞 / 踩 / 取消，写进 `messages.feedback`，消息列表直接回显。

鉴权是完整闭环：VAPTCHA 人机验证 → 邮箱验证码 → 注册 → JWT access + 落库 refresh → 刷新轮换 → 登出撤销，含三层发码限流与账号枚举防护。

---

## 快速开始

```bash
# 1. 依赖（conda 只声明解释器，包版本唯一来源是 requirements*.txt）
conda env create -f environment.yml && conda activate fastapi-learn

# 2. 配置：应用与 docker compose 共用这一个文件
cp .env.example .env          # 填 DB_PASSWORD / REDIS_PASSWORD / MYSQL_ROOT_PASSWORD / LLM_API_KEY

# 3. 起依赖（compose 在根目录，自动读上面的 .env）
docker compose up -d && docker compose ps    # 等两个服务 healthy

# 4. 建表（表结构只由 Alembic 管）
alembic upgrade head

# 5. 跑起来
uvicorn app.main:app --reload --port 8000
```

打开 http://127.0.0.1:8000/docs 即可调试（右上角 Authorize 可填令牌）。

### 聊天前端

仓库包含基于 **Vue 3 + Vite** 的[启学聊天前端](frontend/README.md)，支持登录注册、流式聊天与续传、历史会话和评价。在 `frontend` 目录先执行 `npm ci --ignore-scripts --no-audit --no-fund`，开发运行 `npm run dev`；正式运行 `npm start` 或双击 `start.cmd`，访问 http://127.0.0.1:4318 。Node.js 建议使用 24 LTS。验证码 HTTPS 配置、组件分工和测试方法见前端说明。

验证：

```bash
pytest                       # 全部用例
ruff check . && ruff format --check .
```

---

## 文档

按主题分篇，`README` 只做索引 —— 之前 750 行的单文件已经长到没人愿意滚到底。

| 文档 | 内容 |
| --- | --- |
| [01 架构与分层约定](docs/01-architecture.md) | 目录为什么这么分、每层的职责边界与依赖方向、请求流转、**FastAPI 里几个容易踩的点** |
| [02 环境准备与运行](docs/02-getting-started.md) | conda、MySQL/Redis 容器、配置项、启动、测试与静态检查、四个容器踩坑点 |
| [03 数据层](docs/03-data-layer.md) | 雪花 ID（位布局 / 时钟回拨）、ORM 与仓储的边界、Alembic 作为唯一真相源 |
| [04 接口契约](docs/04-api-contract.md) | 统一响应外壳、错误码编码规则、异常四类通道、对话与 SSE、健康探针、提示词热更新 |
| [05 鉴权与安全](docs/05-auth.md) | 双令牌与轮换、邮箱验证码三层限流、VAPTCHA 服务端二次验签、**防 SQL 注入四层防线**、数据库账号 DML/DDL 分离 |
| [06 下一步](docs/06-roadmap.md) | 按难度递增的后续练习方向 |
| [07 前端接口调用示例](docs/07-api-examples.md) | **给前端的**：TS 类型、可直接用的客户端、VAPTCHA 拿票与发码接入（含票一次性/过期怎么重试）、SSE 解析、错误码处理策略 |

---

## 目录一览

```
sr/
├── app/                     # 应用主包
│   ├── core/                # 配置 / 错误码 / 统一响应 / 中间件 / 安全 / 雪花 ID
│   ├── api/                 # 接口层：router.py 聚合 + deps.py 依赖 + routes/ 一资源一文件
│   ├── schemas/             # 出入参模型（Pydantic）
│   ├── services/            # 业务规则（事务边界在这里）
│   ├── repositories/        # SQL 的唯一落点
│   ├── db/                  # ORM 模型 / 会话 / Redis / 物理类型
│   ├── prompts/             # 提示词"怎么读"：加载、校验、热更新（正文不在这里）
│   └── utils/               # 雪花 ID 解析、IP 编解码、日志脱敏
├── tests/                   # 契约测试（桩 LLM / 桩邮件，不依赖网络）
├── alembic/                 # 迁移脚本
├── deploy/                  # 容器侧配置（MySQL / Redis 的 cnf 与初始化脚本）
├── docs/                    # 本目录文档
├── prompts/                 # 提示词正文（内容资产，不进代码包）
├── web/                     # 零构建联调页（VAPTCHA 单点 + 全链路）
├── docker-compose.yml       # MySQL 8.4 LTS + Redis 7.4
├── requirements.txt         # 运行时依赖
├── requirements-dev.txt     # 开发/测试依赖（-r requirements.txt）
└── .env.example             # 唯一配置模板
```

分层的**理由**（而不是"目录长什么样"）见 [01 架构](docs/01-architecture.md)。

---

## 几条硬约定

- **接口层不写业务**：`api/routes/` 只做协议转换，业务进 `services/`，SQL 进 `repositories/`。
- **表结构只有一个来源**：`alembic/versions/`。容器启动不建表，`deploy/mysql/initdb/` 只放权限脚本。
- **依赖版本只有一个来源**：`requirements*.txt`。`environment.yml` 用 `-r` 引用，不重复列包。
- **配置只有一个来源**：根目录 `.env`。应用与 `docker compose` 读同一个文件，密码不写两遍。
- **内容资产不进代码包**：提示词正文在 `prompts/`，位置由 `PROMPTS_DIR` 决定，容器可挂载覆盖，改文案不必重新发版。
- **错误出口只有两条**：HTTP 状态码（真实状态，不是一律 200）+ 统一外壳里的 `code`。流式接口额外走 SSE `event: error`。

---

## 状态

已完成：统一返回 / 异常处理 / 错误码 / 表结构 + Alembic / 仓储层 / 雪花 ID / 注册登录鉴权（含 VAPTCHA 人机验证）/ 有状态对话（强制登录 + 落库 + 服务端上下文）/ SSE 断线续传 / 会话列表与历史 / 消息评价 / 就绪探针查 DB+Redis。

未做：会话搜索、重新生成回答、忘记密码、RAG、评价数据回灌 —— 见 [06 下一步](docs/06-roadmap.md)。
