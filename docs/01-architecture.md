# 架构与分层约定

为什么这样分目录、请求怎么流转、每一层的边界在哪。

> 返回 [README](../README.md) · 下一篇 [环境准备与运行](02-getting-started.md)

---

## 分层一览

| 层 | 目录 | 职责 | 边界（不该做什么） |
| --- | --- | --- | --- |
| 入口 | `app/main.py` | 应用工厂、中间件、生命周期、挂路由 | 不放业务逻辑 |
| 接口 | `app/api/routes/` | 解析参数、调 service、组装响应 | 不写业务规则，不碰 SQL |
| 契约 | `app/schemas/` | 出入参校验与序列化 | 不掺存储结构，不返回内部字段 |
| 业务 | `app/services/` | 业务规则、事务边界、降级与重试 | 不知道 HTTP 存在，不直接写 SQL |
| 数据 | `app/repositories/` | SQL 的唯一落点 | 不做业务判断 |
| 模型 | `app/db/models.py` | 表结构到对象图的映射 | 不放查询语句 |
| 基础设施 | `app/core/` | 配置、日志、安全、错误码、响应外壳 | 不依赖任何业务模块 |
| 工具 | `app/utils/` | 无状态纯函数 | 不反向依赖上层 |

依赖只能从上往下：`routes → services → repositories → models`。一旦出现反向 import（repository 里引 service、core 里引 routes），通常不是"需要个工具函数"，而是职责划错了。

---

## 目录结构（每个文件夹为什么存在）

```
sr/
├── app/                        # 应用主包
│   ├── __init__.py
│   ├── main.py                 # 入口：应用工厂 + 生命周期 + 中间件 + 挂路由
│   ├── core/                   # 基础设施：配置、ID、错误码、统一响应（与业务无关）
│   │   ├── config.py           # 全局配置（pydantic-settings 读 .env）
│   │   ├── errors.py           # 错误码表 + AppError
│   │   ├── response.py         # 统一响应外壳 ApiResponse[T]
│   │   ├── context.py          # 请求级 ContextVar（trace_id）
│   │   ├── middleware.py       # trace_id 中间件 + 指标采集中间件（纯 ASGI，不缓冲 SSE）
│   │   ├── logging.py          # 结构化日志：JSON / text 两种格式，自动带 trace_id
│   │   ├── metrics.py          # Prometheus 指标定义（独立 Registry，标签用路由模板）
│   │   ├── exception_handlers.py  # 四类异常的集中翻译
│   │   ├── security.py         # 密码哈希（argon2id）+ 令牌签发/校验
│   │   ├── snowflake.py        # 雪花 ID 生成器（时间有序、多实例不冲突）
│   │   └── sse.py              # SSE 事件名与结束哨兵（帧编码交给 sse-starlette）
│   ├── prompts/                # 提示词"怎么读"：加载、校验、热更新（不含正文）
│   │   ├── loader.py           # 加载与校验（不合法启动即失败）
│   │   └── registry.py         # 按 id 取用 + 按 mtime 热更新
│   ├── db/                     # 表结构映射与连接管理
│   │   ├── base.py             # ORM 基类 + 约束命名约定 + utcnow
│   │   ├── enums.py            # 状态枚举，与 CHECK 约束一一对应
│   │   ├── models.py           # 4 张表：users / auth_sessions / conversations / messages
│   │   ├── session.py          # 引擎 / 会话工厂 / get_session 依赖
│   │   ├── redis.py            # Redis 客户端（验证码 / 限流 / 续传缓冲）
│   │   └── types.py            # MySQL 物理类型（UNSIGNED / MEDIUMTEXT / DATETIME(3)）
│   ├── repositories/           # SQL 的唯一落点
│   │   ├── user_repository.py          # 用户读写 + 软删除过滤
│   │   ├── auth_session_repository.py  # refresh token 会话（按哈希查）
│   │   ├── conversation_repository.py  # 会话归属、游标分页、统计更新
│   │   └── message_repository.py       # seq 分配、历史分页、上下文加载、评价
│   ├── api/                    # 接口层
│   │   ├── __init__.py         # 只导出 api_router
│   │   ├── router.py           # 路由聚合：新增资源只改这里
│   │   ├── deps.py             # 依赖装配 + get_current_user
│   │   └── routes/             # 一个资源一个文件
│   │       ├── auth.py         # 发码 / 注册 / 登录 / 刷新 / 登出 / 当前用户
│   │       ├── health.py       # 存活探针 + 就绪探针
│   │       ├── chat.py         # 一次性对话 + SSE 流式 + 断线续传 + 重新生成 + 状态
│   │       ├── conversations.py    # 会话列表 / 详情 / 历史 / 改名 / 软删
│   │       ├── messages.py     # 消息评价
│   │       └── metrics.py      # Prometheus 抓取（根路径 /metrics，不套统一外壳）
│   ├── schemas/                # 接口契约（Pydantic 模型）
│   │   ├── auth.py             # 邮箱归一化 + 密码长度策略
│   │   ├── health.py
│   │   ├── chat.py
│   │   └── conversation.py     # 会话与消息的出入参
│   ├── services/               # 业务逻辑
│   │   ├── exceptions.py       # 业务异常语义
│   │   ├── auth_service.py     # 注册 / 登录 / 刷新 / 登出（事务边界在这里）
│   │   ├── verification_service.py  # 验证码签发与校验 + 三层限流
│   │   ├── vaptcha_service.py  # VAPTCHA 服务端验签（remote / local）
│   │   ├── mail_service.py     # 异步 SMTP 发信
│   │   ├── llm_service.py      # LangChain LCEL 链封装（invoke / astream_events）
│   │   ├── conversation_service.py # 会话归属、上下文组装、落库收尾、重新生成
│   │   ├── context_budget.py   # token 估算 + 按预算裁剪历史
│   │   ├── chat_throttle.py    # 成本闸门：频率 / 在途并发 / 日 token 额度
│   │   └── stream_hub.py       # SSE 分片缓冲 + 生产者任务 + 看门狗 + 断线续传
│   └── utils/                  # 通用工具
│       ├── ids.py              # 路径里的雪花 ID 解析（只收纯数字）
│       ├── net.py              # IP ↔ VARBINARY(16)
│       └── masking.py          # 日志脱敏（邮箱 / 令牌）
├── tests/                      # 测试
│   ├── conftest.py             # TestClient + LLM 桩 + 邮件桩 + 测试库隔离
│   ├── test_health.py          # 探针 + 统一响应外壳
│   ├── test_chat.py            # 一次性 / 流式对话
│   ├── test_prompts.py         # 提示词加载与热更新
│   ├── test_errors.py          # 错误码编码规则 + 四类异常路径
│   ├── test_auth.py            # 注册 / 登录 / 刷新 / 登出 / 账号枚举防护
│   ├── test_security.py        # 密码哈希 + JWT（含 alg=none 伪造）
│   ├── test_verification.py    # 验证码一次性 / 过期 / 冷却 / 双层配额
│   ├── test_vaptcha.py         # 验签契约（remote / local / 失效 / 降级）
│   ├── test_sql_injection.py   # 源码 AST 扫描 + 运行时注入载荷
│   ├── test_snowflake.py       # 并发唯一性 + 位布局 + 时钟回拨
│   ├── test_models.py          # 纯 metadata 断言（物理类型 / 约束名）
│   ├── test_db_contract.py     # 连真库比对 information_schema + 权限收权校验
│   ├── test_conversations.py   # 会话列表 / 历史 / 归属 404
│   ├── test_stream_resume.py   # 续传：id 帧 / 重放 / 续读 / 过期 / 无人接收 / 心跳独立性
│   ├── test_context_budget.py  # token 估算 + 预算裁剪
│   ├── test_chat_throttle.py   # 成本闸门：并发 / 频率 / 日额度 / 429 不落库
│   └── test_mysql_driver.py    # PyMySQL 与 aiomysql 的转义兼容
├── alembic/                    # 数据库迁移（表结构的唯一真相源）
│   ├── env.py                  # 复用 app 的配置与模型，DSN 不写进 ini
│   ├── script.py.mako          # 迁移模板（已改成项目风格，生成即过 ruff）
│   └── versions/               # 迁移脚本，只依赖 sqlalchemy，不 import 应用代码
├── deploy/                     # 容器侧配置（只有配置文件，没有应用代码）
│   ├── mysql/conf.d/01-server.cnf  # 字符集 / 时区 / 慢查询 / sql_mode
│   ├── mysql/initdb/10-grants.sh   # 账号收权：运行时只给 DML，DDL 归迁移账号
│   └── redis/redis.conf        # AOF / volatile-lru / ACL 收权
├── docs/                       # 文档（README 只做索引，细节按主题分篇）
│   ├── 01-architecture.md      # 目录分层、请求流转、每层的边界
│   ├── 02-getting-started.md   # 环境准备、容器、配置、运行、测试
│   ├── 03-data-layer.md        # 雪花 ID / 仓储 / Alembic
│   ├── 04-api-contract.md      # 统一响应 / 错误码 / SSE / 探针 / 提示词
│   ├── 05-auth.md              # 双令牌 / 验证码 / 防注入 / 账号权限
│   ├── 06-roadmap.md           # 后续练习方向
│   └── 07-api-examples.md      # 前端调用示例（TS 类型 + 客户端 + SSE 解析）
├── prompts/                    # 提示词正文（内容资产，不属于代码包）
│   └── chat_system.md          # 正文 + 元数据（id / version / description）
├── web/                        # 零构建联调页（VAPTCHA 单点 + 全链路）
├── docker-compose.yml          # MySQL 8.4 LTS + Redis 7.4（+ profile=app 的应用容器）
├── Dockerfile                  # 应用镜像：多阶段、非 root、tini、HEALTHCHECK
├── .github/workflows/ci.yml    # lint / test（真 MySQL+Redis）/ image
├── environment.yml             # conda 环境：只声明解释器，用 -r 引用 requirements
├── requirements.txt            # 运行时依赖（范围）
├── requirements.lock           # 精确版本（生产镜像装它）
├── requirements-dev.txt        # 开发/测试依赖（-r requirements.txt）
├── pyproject.toml              # 项目元信息 + pytest / ruff 配置
├── .env.example                # 唯一配置模板（应用与 compose 共用）
├── .gitignore
└── .gitattributes              # 统一 LF（容器挂载的 .cnf/.conf 对 CRLF 敏感）
```

### 各层存在的理由（按依赖顺序）

**`app/`** — 所有代码收在一个可导入的包内。`uvicorn app.main:app` 按模块路径找应用对象，代码放到包外就是 `ModuleNotFoundError`；把仓库根留给配置、文档和编排文件，import 路径也不会随目录调整而变。

**`app/core/`** — 配置、日志、安全、异常处理这些东西每个项目都要，但不属于任何业务。混在业务包里会导致：换环境时要全局搜索改代码；想复用到下一个项目得连业务一起搬。所以单独成层，业务只"使用"它。

**`app/api/`** — 只做 HTTP 协议转换：收参数 → 调 service → 返回。**禁止写业务规则**，因为同一段业务还要被定时任务、消息消费者调用，写在接口层就被绑死在 HTTP 上。

**为什么叫 `api/routes/` 而不是 `controllers/`** — 这是 FastAPI 生态的命名惯例：官方教程用 `routers/`，全栈模板用 `api/routes/`，`controllers` 是别的框架的词。跟着生态走，接手的人不用先猜你的目录约定。

**版本号只留在 URL 里** — `/api/v1` 是**对外契约**，不兼容变更只能 v1、v2 并存；但它不该用目录嵌套表达。目录结构属于内部实现，哪天把 `routes/` 拆成 `routes/admin/`、`routes/open/`，URL 不该跟着变。所以这里是 `api/routes/chat.py` + `API_PREFIX=/api/v1`，而不是 `api/v1/endpoints/chat.py`。

**`app/schemas/`** — 出入参契约。独立于任何存储结构：接口返回常常是拼装结果，且内部字段（密钥、审计字段）绝不能返回给前端。Pydantic 模型同时承担运行时校验和 OpenAPI 文档，所以它只该描述"接口长什么样"，不该描述"表长什么样"。

**`app/repositories/`** — SQL 的唯一落点，一表一仓库。存在的意义有两个：一是"防注入"这条规则只需要在一个地方守住（见[防 SQL 注入](05-auth.md#防-sql-注入四层参数绑定只是第一层)）；二是软删除过滤、字段白名单这类**漏一次就是漏洞**的细节，写进仓储就不会被某个调用方忘掉。

**`app/services/`** — 业务规则的唯一归宿：可复用、可单测（不依赖 HTTP）、隔离框架（换 Web 框架不用重写业务）。LLM 的链构建、降级、超时重试，以及注册/登录的事务边界，全部收敛在这里，接口层只做协议适配。

**`tests/`** — 测试锁住的是**接口契约**而不是实现。LLM 用桩链、邮件用桩服务替换，不花钱、不依赖网络、结果可断言。

**`prompts/`（在 `app/` 之外）** — 提示词是**内容资产**，不是代码。放包里会带来三个具体问题：改一句文案要重新打包发版；`.md` 混进代码目录，搜索代码时被噪声淹没；非开发人员得进源码树才能改。所以正文放仓库根，位置由 `PROMPTS_DIR` 决定（可指向容器挂载的绝对路径），`app/prompts/` 只保留"怎么读、怎么校验、怎么热更新"。

---

## 请求流转

```
HTTP → Starlette 路由匹配 → APIRouter（routes）
     → Depends 解析依赖 → Service → 上游 LLM 网关
```

路径：`/api/v1/chat/stream` 由三段拼出来 —— `settings.API_PREFIX` + `router.py` 的 `prefix="/chat"` + 路由模块里的 `"/stream"`。资源根路径只在一处定义，改前缀不会漏。

---

## 几个容易踩的点

### 1. 路由不会自动注册

`APIRouter` 只是个容器，模块里写了 `router` 不代表它被挂到应用上。必须在 `app/api/router.py` 里 `include_router`，再在 `main.py` 里挂一次。**漏写就是 404，而且启动不报错** —— 新增资源时最典型的"写完没生效"。

### 2. 依赖不是单例

`Depends` 的语义是"处理这个请求前先调一下这个函数"，不是"从容器里取一个装配好的对象"：

| 关注点 | FastAPI 的行为 |
| --- | --- |
| 声明位置 | 函数参数 |
| 解析时机 | 每个请求按需解析 |
| 默认作用域 | 每请求调用一次（不是单例） |

`service: LLMService = Depends(get_llm_service)` 读作："处理请求前先调 `get_llm_service()`，把返回值传进来。"

⚠️ **后果**：如果工厂函数里每次 `return XxxService()`，每个请求都会新建对象，对象级缓存立刻丢。所以 `get_llm_service()` 用 `lru_cache(maxsize=1)` 做成进程级单例 —— LLM 客户端内部持有 httpx 连接池，每请求新建等于每请求新建连接池。

### 3. 参数校验靠类型

```python
message: str = Field(min_length=1, max_length=8000)
title: str = Field(min_length=1, max_length=100)
```

规则直接写在类型标注上。Pydantic 一次声明同时得到三样东西：运行时校验、422 的字段级错误、OpenAPI 里的 schema —— 不需要再维护一份校验代码，也就不会出现"文档和校验不一致"。

### 4. 异常处理

service 层**不抛 HTTP 异常**，只抛 `AppError` 子类（带业务错误码，如 `LLMNotConfiguredError` / `LLMUpstreamError`）；由 `exception_handlers.py` 统一翻译成状态码 + 统一响应体。原因：service 不该知道 HTTP 语义，否则换框架就要重写业务。

放在全局的好处是路由里**不需要写 `try/except`**。逐个路由兜异常，漏一个就是 500 加一段栈；全局处理器不会漏，也不会出现"这个接口的错误格式跟别的接口不一样"。

细节见[统一响应结构](04-api-contract.md#统一响应结构)和[异常怎么走](04-api-contract.md#异常怎么走)。

---
