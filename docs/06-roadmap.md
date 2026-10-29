# 下一步

按难度递增的后续练习方向。

> 返回 [README](../README.md) · 上一篇 [鉴权与安全](05-auth.md) · 下一篇 [前端接口调用示例](07-api-examples.md)

---

## 下一步可以练什么

按由易到难（统一返回、异常处理、错误码、表结构、迁移、仓库层、鉴权已经做完了，下面接在后面）：

1. ~~**SSE 断线续传**~~ ✅：生成与连接解耦 —— 生产者任务独立跑、每块 `XADD` 进 Redis Stream；每帧带 `id:`（Stream entry id），客户端重连时用 `Last-Event-ID` 调 `GET /chat/stream/{message_id}` 续读，不重新生成、不重复计费。配套两个界：无人接收超过 `SSE_RESUME_GRACE_SECONDS` 就停生成（落 `interrupted`），缓冲只留 `SSE_RESUME_TTL_SECONDS`（过期 410）。
2. **LLM 结构化输出**：`.with_structured_output(PydanticModel)` 替换 `StrOutputParser`，做实体抽取。
3. ~~**对话接口挂鉴权**~~ ✅：`/chat`、`/chat/stream` 用 `CurrentUser` 强制登录，没令牌直接 `401 / 11401`；`user_id` 全从令牌取，会话与消息的归属条件写进 WHERE。**没有**保留匿名模式 —— 上下文只认服务端，请求体里的 `history` 字段已删除（让浏览器决定喂给模型什么上下文，等于把提示注入的入口开在客户端）。因此历史消息列表 / 续聊 / 评价全部只对本人可见。
4. ~~**流式落库**~~ ✅：流开始前先写用户消息 + 一条 `status=STREAMING` 的助手占位，流结束回填 `COMPLETE`；**没有客户端接收**（grace 到期）或进程停机标 `INTERRUPTED`（已生成的部分保留），上游报错标 `FAILED`。增量分片不进 MySQL —— 落库只在开头和结尾各一次，中间的分片放 Redis Stream（第 1 条），避免每块一次写库。
5. **会话搜索**：`title` 和消息正文的全文检索（`LIKE` 或 MySQL 全文索引），配合会话列表的游标分页做"搜索后跳转"。
6. ~~**就绪探针加 DB / Redis**~~ ✅：现在 `/health/ready` 真跑一次 `SELECT 1` 和 `PING`，失败返回 503。
7. ~~**登录失败次数限制**~~ ✅：验证码侧有三层限流，密码登录侧也有了 —— 按账号 / IP 双计数，验证密码前先检查计数。
8. **忘记密码**：`VerificationScene` 加一个 `RESET_PASSWORD`（Redis key 自动隔离），复用同一套发码与校验，再加一个 `POST /auth/reset-password`。表里再加 `email_verified_at` 就能支撑"改邮箱需重新验证"。
9. ~~**人机验证**~~ ✅：VAPTCHA V4 接在 `AuthService.send_register_code()` 最前面（消耗配额之前验签），支持 `remote` 官方接口与 `local` 本地 HMAC 两种模式，前端联调页在 `web/`。
10. **评价数据回灌**：把 `feedback=down` 的问答捞出来做 badcase 集，用于回归测试与提示词调优（`idx_msg_feedback` 索引就是为这个建好的）。
11. **加 RAG**：文档切分 → 向量库（如 pgvector / Chroma）→ retriever 接进链，`prompt | retriever` 组合。
12. ~~**重新生成回答**~~ ✅：`POST /chat/regenerate/{message_id}`，SSE 帧格式与 `/chat/stream` 一致。**不重复写用户提问**，只在末尾追加一条新回答，旧回答保留（比较 / 评价 / badcase 都要用），新消息用 `parent_message_id` 指向被替代的那条；上下文加载据此排除旧回答（相关 `EXISTS` 子查询，配 `idx_msg_parent`）。
13. ~~**对话成本闸门**~~ ✅：三道闸都在写库之前拦 —— 每用户每分钟轮次（`CHAT_RATE_LIMIT_PER_MINUTE`）、同时在途生成数（`CHAT_MAX_CONCURRENT_STREAMS`，用 ZSET 存过期时刻，进程崩溃也能自动回收名额）、每日 token 预算（`CHAT_DAILY_TOKEN_BUDGET`）。输出侧加 `LLM_MAX_TOKENS` 硬上限。超限一律 `429 / 22429`。
14. ~~**可观测性**~~ ✅：结构化日志（`LOG_FORMAT=json`，自动带 trace_id，`LOG_LEVEL` 显式配置）+ `/metrics`（在途流、首字延迟、轮次耗时、token 用量、错误码分布、被闸门拒绝次数、HTTP 时延）。**首字延迟单独一个直方图**：用户抱怨"卡"多半是 TTFT，而优化 TTFT 与优化总耗时是两件不同的事。
15. **分布式追踪（OTel）**：日志里的 trace_id 能串起单条请求，但跨服务（网关 → 应用 → LLM → 数据库）要 `opentelemetry-sdk` + exporter。注意 SSE 的 span 要覆盖整条流，否则看起来永远"很快"。
16. **上下文压缩**：现在超预算就丢最旧的消息；更进一步是摘要化（把早期对话压成一段 system 提示），代价是多一次模型调用。
