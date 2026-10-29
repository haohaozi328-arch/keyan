# 环境准备与运行

conda 环境、MySQL/Redis 容器、配置项、启动与测试。

> 返回 [README](../README.md) · 上一篇 [架构与分层约定](01-architecture.md) · 下一篇 [数据层：雪花 ID / 仓储 / 迁移](03-data-layer.md)

---

## 环境准备（conda）

```powershell
# 方式一：environment.yml（推荐）
#   它只声明解释器 + pip，包列表用 -r 引用 requirements-dev.txt，不会两处漂移
conda env create -f environment.yml
conda activate fastapi-learn

# 方式二：手写两行
conda create -y -n fastapi-learn python=3.11
conda activate fastapi-learn
pip install -r requirements-dev.txt
```

> **为什么要单独建环境**：FastAPI 新版要求 Python ≥3.10，本机 `sy1` 是 Python 3.8.20，装不了新版本。已建好的 `fastapi-learn` 用 Python 3.11。
>
> 若 pip 报 SSL 错误，换镜像：
> `pip install -r requirements-dev.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com`
>
> **两个 requirements 文件怎么选**：本地开发和 CI 用 `requirements-dev.txt`（它 `-r requirements.txt`，额外带 pytest / httpx / ruff）；**部署到服务器只装 `requirements.txt`**，不要把测试工具带进生产镜像。

## 本地依赖（MySQL + Redis）

数据库与缓存用 Docker 起。**compose 文件在仓库根目录**，和 `.env` 同级：

```
sr/
├── docker-compose.yml          # MySQL 8.4 LTS + Redis 7.4
├── .env.example                # 应用与 compose 共用的唯一配置模板
└── deploy/                     # 只放容器配置文件，不含 compose 与 env
    ├── mysql/conf.d/01-server.cnf  # 字符集 / 时区 / 慢查询 / sql_mode
    ├── mysql/initdb/10-grants.sh   # 账号收权（DML / DDL 分离）
    └── redis/redis.conf            # AOF / volatile-lru / ACL 收权
```

> **为什么 compose 不放 `deploy/`**：`docker compose` 只自动读取**compose 文件同级目录**的 `.env` 做变量插值。放在根目录，应用（`app/core/config.py`）和容器编排读的是同一个文件，`DB_PASSWORD` 只写一次；compose 里的 `MYSQL_USER` / `MYSQL_PASSWORD` 直接引用 `DB_USER` / `DB_PASSWORD`，不再有第二套变量。
>
> 之前 `deploy/.env` 和根 `.env` 各存一份密码，改密码必须两边同步 —— 漏一边就是"应用能连、容器起不来"或反过来的经典事故。

```powershell
cd D:\workspace\python\sr
cp .env.example .env           # 填 DB_PASSWORD / REDIS_PASSWORD / MYSQL_ROOT_PASSWORD
docker compose up -d           # 只起容器，此时库里是空的
docker compose ps              # 等到两个服务都 healthy

alembic upgrade head           # 建表（表结构由 Alembic 管理，不在容器启动时建）
alembic check                  # 应输出 No new upgrade operations detected
```

几个容易踩的点：

- **Windows 上 bind mount 的配置文件会被 MySQL 忽略**。挂进去的文件权限恒为 `0777`，MySQL 见到 world-writable 直接跳过，只在日志里留一行 `World-writable config file ... is ignored`，进程照常启动。后果是 `time_zone` 还是 `SYSTEM`、`slow_query_log` 还是 0、`max_connections` 还是 151——**看起来一切正常，其实配置全没生效**。所以 compose 里把配置挂到 `conf.d-src`，由 entrypoint 用 `install -m 0644` 复制到 `conf.d` 再启动。验证方法：`SELECT @@time_zone, @@slow_query_log, @@max_connections;`

- **Redis 的 ACL 规则不能走命令行参数**。Redis 会把命令行值整体 `sdscatrepr` 加引号后当成一个 token，ACL 解析时用户名里就带了空格，直接报 `Spaces not allowed in ACL usernames` 并拒绝启动。所以 compose 里由 entrypoint 渲染出临时 aclfile 再用 `--aclfile` 加载。
- **表结构只由 Alembic 管，容器启动时不再建表**。之前 `docker-entrypoint-initdb.d` 里放了一份 DDL，结果和模型在注释、索引上大面积漂移，`schema_migrations` 还和 `alembic_version` 打架。删掉重复定义后，`alembic check` 才能作为"模型和库是否一致"的守门员。想从零重来：`docker compose down -v` 后重新 `up -d` + `alembic upgrade head`（`-v` 会连数据一起删）。
- **MySQL 8.4 默认认证插件是 `caching_sha2_password`**，PyMySQL / aiomysql 需要装 `cryptography` 才能握手，否则报 `RuntimeError: 'cryptography' package is required`。8.4 已经移除了 `--default-authentication-plugin`，不要指望退回 `mysql_native_password`。
- **雪花 ID 出接口必须转字符串**。`BIGINT` 超过 2^53，前端 `JSON.parse` 会把末几位变成 0，拿到的 ID 直接查不到记录。Pydantic 里用 `field_serializer` 或直接声明 `str`。
- **PyMySQL 必须钉 `<1.2`**。1.2 起把 `converters.escape_bytes_prefixed` 换成了字符串占位符（值就是 `"DO NOT IMPORT THIS!!!"`），唯一目的是让 aiomysql 的 `from ... import` 不报 ImportError；而 aiomysql 0.3.2 仍然把它当函数调用，于是**任何带 bytes 参数的 SQL 都会炸**：`TypeError: 'str' object is not callable`。本项目 `User.last_login_ip` / `AuthSession.ip` 是 `BinaryIP`（bytes），所以注册走到签发 token 那步必然 500 —— 而且只在真连 MySQL 时现形，用 SQLite 跑的 200 多条单测全绿也发现不了。
  `app/db/session.py` 的 `_patch_aiomysql_escape()` 会在建引擎前把名字换回可调用的 `escape_bytes` 兜底，`tests/test_mysql_driver.py` 造了"坏版本"状态验证这个补丁。但依赖侧仍然要钉版本，别指望补丁帮你兜一辈子。

## 配置项（.env）

`LLM_API_KEY`（不填 = 功能关闭，主服务不受影响）、`LLM_BASE_URL`（OpenAI 兼容网关）、`LLM_MODEL`、`LLM_TIMEOUT_SECONDS`、`LLM_MAX_RETRIES`、`LLM_TEMPERATURE`、`SSE_PING_SECONDS`、`PROMPT_CHAT_SYSTEM_ID`（用哪份提示词）、`PROMPTS_DIR`（提示词目录，默认 `prompts`）。提示词正文不在配置里，在 `prompts/*.md`。

安全要点：历史消息 role 白名单只允许 `user`/`assistant` —— 客户端伪造 `system` 角色会被 422 拦截（防提示注入）；系统提示词仅由服务端配置注入。

```powershell
# 未配置密钥时
curl.exe http://127.0.0.1:8000/api/v1/chat/status
# {"code":0,"message":"ok","data":{"configured":false,...,"prompt_id":"chat_system"},"trace_id":"..."}

# 流式对话（-N 关闭 curl 自身缓冲，才能看到逐块输出）
curl.exe -N -X POST http://127.0.0.1:8000/api/v1/chat/stream -H "Content-Type: application/json" -d "{\"message\":\"你好\"}"
```

## 运行

```powershell
conda activate fastapi-learn
cd D:\workspace\python\sr

# 配置密钥（不配也能启动，LLM 接口返回 503）
cp .env.example .env

# 启动服务
uvicorn app.main:app --reload --port 8000
```

| 地址 | 说明 |
| --- | --- |
| http://127.0.0.1:8000/docs | Swagger UI，可直接调试接口（右上角 Authorize 可填令牌） |
| http://127.0.0.1:8000/redoc | ReDoc 文档 |
| http://127.0.0.1:8000/api/v1/health | 存活探针（liveness） |
| http://127.0.0.1:8000/api/v1/health/ready | 就绪探针（readiness） |
| POST http://127.0.0.1:8000/api/v1/auth/verification-code | 发邮箱验证码 |
| POST http://127.0.0.1:8000/api/v1/auth/register | 邮箱注册 |
| POST http://127.0.0.1:8000/api/v1/auth/login | 邮箱密码登录 |
| POST http://127.0.0.1:8000/api/v1/auth/refresh | 刷新令牌 |
| POST http://127.0.0.1:8000/api/v1/auth/logout | 登出 |
| GET http://127.0.0.1:8000/api/v1/auth/me | 当前登录用户 |
| http://127.0.0.1:8000/api/v1/chat/status | 对话功能状态（是否已配置密钥） |
| POST http://127.0.0.1:8000/api/v1/chat | 对话，一次性返回 |
| POST http://127.0.0.1:8000/api/v1/chat/stream | 对话，SSE 流式返回 |
| GET http://127.0.0.1:8000/api/v1/chat/stream/{message_id} | 断线续传 |
| POST http://127.0.0.1:8000/api/v1/chat/regenerate/{message_id} | 重新生成一条回答（SSE） |
| http://127.0.0.1:8000/metrics | Prometheus 指标（根路径，不在 /api/v1 下） |

> 端口注意：本机 **8010 被系统保留**，绑定会报 WinError 10013，用 8000 即可。
> 若 8000 被旧进程占用（报 `[Errno 10048]`），先找到并停掉：
> `Get-NetTCPConnection -LocalPort 8000 -State Listen` → `Stop-Process -Id <PID>`。

## 测试与静态检查

```powershell
conda activate fastapi-learn
cd D:\workspace\python\sr

pytest                              # 全部用例
ruff check .                        # 静态检查
ruff format --check .               # 格式检查（CI 用）
ruff format .                       # 真正格式化
```

测试用桩链替换 LLM 服务、用桩邮件服务截获验证码（`app.dependency_overrides[...]`），不依赖外部网络，随时可跑。

`tests/test_db_contract.py` 是**契约测试**：连真实库，把 `information_schema` 里的列类型、索引、约束名跟 `Base.metadata` 逐项比对，另外校验运行时账号确实没有 DDL 权限。库没起时自动 skip，起了就一定跑 —— 这是唯一能拦住"模型改了、迁移没生成""权限被悄悄放开"这类漂移的东西。

鉴权相关用例需要 MySQL + Redis：注册要真写库（唯一约束、事务边界、级联删除用假的测不出来），验证码要真 TTL 和真 `INCR`。跑在**独立的 Redis db 15** 上，用 `SCAN` + `DEL` 清理（`FLUSHDB` 属于 `@dangerous`，被 ACL 拒），MySQL 侧只删邮箱前缀为 `sr-test-` 的行，不碰开发数据。任一后端不可达时整组 skip。

> 注意：如果清理 Redis 失败，测试会**直接退出**而不是继续跑。否则配额计数会跨轮次累积，表现为"单跑能过、连着跑就 429"，极难定位。

## 打包与 CI

### 应用镜像

```bash
docker build -t agent-chat .
# 或者直接用编排里的应用服务（会连 compose 里的 mysql / redis）
docker compose --profile app up -d --build
```

镜像里是**锁定的**依赖（`requirements.lock`）：同一份代码在任何时间构建出来的依赖集合都一样。CI 里的测试则装 `requirements.txt` 的范围，让上游的新版本先在那里暴露问题 —— 两者分工不同，别把 CI 装的东西当成生产装的东西。

镜像本身的几个决定：多阶段构建（编译工具链不进运行镜像）、非 root（uid 10001）、`tini` 接管 PID 1（uvicorn 才能收到 SIGTERM 走优雅停机，等在途 SSE 把 `interrupted` 写回库）、`HEALTHCHECK` 只打 liveness（依赖抖动不该触发重启）。

### CI（.github/workflows/ci.yml）

| job | 内容 |
| --- | --- |
| `lint` | `ruff check` + `ruff format --check` |
| `test` | 真 MySQL 8.4 + 真 Redis（带密码，否则相关用例整体 skip）+ `alembic upgrade head` + `alembic check` + 全量 `pytest` |
| `image` | `docker build`，防止 Dockerfile 悄悄坏掉（漏 COPY、路径失效） |

CI 里的 MySQL 账号是**手工收权的**，和 `deploy/mysql/initdb` 保持一致：`app` 只有 DML，DDL 归 `migrator`。用 root 跑测试会让"运行时账号不能有 DDL 权限"那条断言失真 —— 而它正是防注入的第二道墙。

`alembic check` 是防漂移的关键一步：改了模型却忘了生成迁移，单元测试永远自洽（它们用的是模型），只有这一步能发现库里缺列。
