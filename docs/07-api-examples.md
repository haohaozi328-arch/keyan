# 前端接口调用示例

给前端用的完整调用说明：请求/响应实例都取自**真实运行结果**，不是手写示意。

> 返回 [README](../README.md) · 上一篇 [下一步](06-roadmap.md)

---

## 0. 三个先记住的约定

**① Base URL 带版本前缀**

```
http://127.0.0.1:8000/api/v1
```

本地开发时 CORS 只放行了 `http://localhost:5173` / `http://127.0.0.1:5173`（Vite 默认端口）。换端口要改后端 `.env` 的 `CORS_ORIGINS`。

**② 所有 JSON 接口同一个外壳，HTTP 状态码是真实的**

```json
{ "code": 0, "message": "ok", "data": { }, "trace_id": "136d0109..." }
```

`code = 0` 表示业务成功。**失败时 HTTP 状态码也是真的**（401 就是 401、422 就是 422），不是"一律 200 把错误塞进 body"。所以：

- 不要只看 `res.ok`，也不要只看 `code` —— 两者一起看；
- 报障时把 `trace_id` 一起给出，服务端日志按它串联。

**③ 雪花 ID 是字符串**

用户 ID 是 19 位整数，超过 JS 的 `Number.MAX_SAFE_INTEGER`（16 位）。后端已经序列化成字符串，前端**不要 `Number(id)`**，否则末几位被静默改写，表现为"用户 ID 对不上"。

---

## 1. 能力清单

| 分组 | 接口                             | 说明                            |
| -- | ------------------------------ | ----------------------------- |
| 鉴权 | `POST /auth/verification-code` | 给邮箱发 6 位验证码，**需带 VAPTCHA 四件套**（见 2.1.1）   |
|    | `POST /auth/register`          | 邮箱 + 密码 + 码，**成功直接返回令牌**（201） |
|    | `POST /auth/login`             | 邮箱 + 密码                       |
|    | `POST /auth/refresh`           | refresh 换新的一对，旧的立刻作废          |
|    | `POST /auth/logout`            | 撤销 refresh token（幂等）          |
|    | `GET /auth/me`                 | 校验登录态 / 取当前用户                 |
| 对话 | `GET /chat/status`             | LLM 是否配置、当前提示词版本，不打真网络（**不需要登录**）        |
|    | `POST /chat`                   | 一次性返回完整回答，**需登录**             |
|    | `POST /chat/stream`            | SSE 逐块返回，**需登录**               |
|    | `GET /chat/stream/{message_id}` | 断线续传：从 `Last-Event-ID` 之后接着发（见 3.3.1、3.3.2） |
|    | `POST /chat/regenerate/{message_id}` | 重新生成一条回答（SSE，见 3.3.3）         |
| 会话 | `GET /conversations`           | 我的会话列表（游标分页，最近活跃优先）           |
|    | `GET /conversations/{id}`      | 会话详情                          |
|    | `GET /conversations/{id}/messages` | 历史消息（按 seq 升序，可往前翻页）        |
|    | `PATCH /conversations/{id}`    | 改名                            |
|    | `DELETE /conversations/{id}`   | 软删除（幂等）                       |
| 消息 | `POST /messages/{id}/feedback` | 评价助手回复：赞 / 踩 / 取消              |
| 健康 | `GET /health`                  | 存活探针                          |
|    | `GET /health/ready`            | 就绪探针                          |

> **对话接口要求登录**。`/chat`、`/chat/stream` 都必须带 `Authorization`，没带就是 `401 / 11401`，不存在匿名试用。历史上下文由服务端按 `conversation_id` 加载，请求体里**没有** `history` 字段。

---

## 2. 鉴权

### 2.1 发验证码

`POST /auth/verification-code`

```json
{ "email": "you@qq.com", "scene": "register" }
```

`scene` 可省略，默认 `register`。

**人机验证（VAPTCHA）**：服务端配置了 `VAPTCHA_VID/VKEY` 后，请求体必须带上前端 SDK `validate()` 成功返回的四件套（字段同名，前端零转换）：

```json
{
  "email": "you@qq.com",
  "token": "<vaptcha token>",
  "knock": "<vaptcha knock>",
  "dfu": "<设备指纹，可选>",
  "ip": "<SDK 返回的签名 IP，可选>"
}
```

服务端未配置 VAPTCHA 时这些字段整体可省略。验签不过 → `400 / 11400`（"人机验证未通过，请重新验证"）；验签服务本身超时 → `504 / 21504`，此时**不发码**（fail-closed，防止"打挂验签就免验证"）。

> 这四个值怎么拿到、票为什么不能复用，见下面的 2.1.1。

成功（200）：

```json
{
  "code": 0,
  "message": "验证码已发送，请查收邮件",
  "data": { "email": "you@qq.com", "expires_in": 300, "resend_after": 60 },
  "trace_id": "db10a747..."
}
```

前端要点：

- **不返回验证码本身**，也不告诉你"这个邮箱是否已注册"（防账号枚举）。所以别做"邮箱已占用"的即时校验，只能等提交注册时才知道。
- `resend_after = 60`：60 秒冷却，拿它做倒计时按钮，别自己写死 60。
- 60 秒内重发返回 **429 / code 11429**，`message` 会带剩余秒数。
- 同一邮箱每天 10 次、同一 IP 每小时 20 次（可配）。

#### 2.1.1 四件套从哪来（SDK 侧，不在接口里）

服务端只认 `token` / `knock` / `dfu` / `ip` 四个字段，这四个值是 VAPTCHA SDK 的 `validate()` 吐出来的。前端**不做任何加工，字段名原样传**。

```html
<!-- 1) 引入 SDK：只有这一行，不用 npm 装 -->
<script src="https://c4.vaptcha.com/src/v4.js"></script>
```

```ts
const VID = "id_xxxxxxxx"; // 验证单元的公开 ID，放前端没问题
let vaptchaObj: any = null;

// 2) 用 VID 初始化。container 只用来定位，别给它设高度 —— 面片挂在 SDK 自己的全屏遮罩里，不在这个容器内
async function initCaptcha() {
  vaptchaObj = await (window as any).vaptcha({
    vid: VID,
    container: "#vaptcha-container",
    lang: "zh-CN",
  });
}

// 3) 用户点「发送验证码」时才验证
async function getTicket() {
  if (!vaptchaObj) await initCaptcha();
  const r = await vaptchaObj.validate(); // 用户取消也走这里，返回空对象
  if (!r?.token || !r?.knock) throw new Error("验证未通过或被取消");
  return { token: r.token, knock: r.knock, dfu: r.dfu ?? "", ip: r.ip ?? "" };
}

// 4) 先拿票再发码
async function requestCode(email: string) {
  const captcha = await getTicket(); // 这里抛错 = 用户取消，直接 return，别发请求
  return api.sendCode(email, captcha);
}
```

**三条纪律，踩了就是 `400 / 11400`**

1. **别提前拿票。** `token` 第一段就是签发时间戳，票的有效窗口很短（官方口径 30 秒）。页面加载时预取、等用户慢慢填完邮箱再发，基本已经过期。拿到票就立刻发码。
2. **票是一次性的。** 不管发码成功还是失败（含 429 冷却、504 验签挂了），这张票都作废了。重试必须重新走 `validate()`。这条最难自己发现：用户看着「我明明验证过了」，服务端只会回「人机验证未通过」。
3. **VKey 不出现前端。** 前端只有 `VID`（公开值）。`VKey` 是服务端验签的密钥，泄露等于任何人都能自己签票给自己。

**四个字段都是纯字符串**，长度上限：`token` 1024 / `knock` 256 / `dfu` 2048 / `ip` 64。按字段取出来传，别把整个 `validate()` 返回值 `JSON.stringify` 后塞进去（超长会 422）。

**按 message 分流（同一个 11400 有三种含义）**

| HTTP / code | message 关键词 | 前端动作 |
| --- | --- | --- |
| 400 / `11400` | 缺少人机验证票据 | 请求体漏了 `token` / `knock`。检查有没有真的把四件套 spread 进去 |
| 400 / `11400` | 人机验证未通过 | 票废了（过期 / 复用 / IP 变了）→ 重新 `validate()` 再发一次 |
| 400 / `11400` | 人机验证已过期 | 同上，票超时 |
| 504 / `21504` | — | 验签服务不可达，**后端没发码**（fail-closed）。这不是用户操作问题，提示「稍后重试」，别让用户反复刷验证 |
| 429 / `11429` | 带剩余秒数 | 60 秒冷却，用 `resend_after` 做倒计时 |
| 422 | — | 字段类型不对（比如 `token: null`），属于传参 bug |

**两个环境前提：做不到会卡在初始化，跟 VID 无关**

- **页面必须 HTTPS。** http 页面调 `v4c.vaptcha.com/api/config` 一律返回 `code:9`，SDK 报「当前网站未授权」，换 VID、加白名单都没用。本地联调怎么搭 HTTPS 见本文 8.1。
- **PC 端 Edge 要避开弹窗分支。** SDK 默认用 `window.open` 开独立验证窗口，Edge 里那个窗口收不到父页面的 boot，会永远停在「验证准备中…」。8.1 有现成的内嵌补丁；生产环境优先在 VAPTCHA 控制台把验证单元设成内嵌形态，而不是在业务代码里 patch SDK。

### 2.2 注册

`POST /auth/register` → **HTTP 201**

```json
{
  "email": "you@qq.com",
  "password": "至少8位",
  "code": "123456",
  "nickname": ""
}
```

| 字段         | 规则                                       |
| ---------- | ---------------------------------------- |
| `email`    | 合法邮箱，服务端统一转小写                            |
| `password` | 8–128 位，**不限字符类型**（NIST 建议：不强制大小写数字符号混排） |
| `code`     | 必须是 6 位数字                                |
| `nickname` | 可省略，最长 64；留空则取邮箱 `@` 前缀（`you@qq.com` → `you`） |

成功（201）：

```json
{
  "code": 0,
  "message": "注册成功",
  "data": {
    "user": {
      "id": "1857349201835921408",
      "email": "you@qq.com",
      "nickname": "you",
      "created_at": "2026-10-28T07:58:49Z"
    },
    "token": {
      "access_token": "eyJhbGciOiJIUzI1NiIs...",
      "refresh_token": "9tK3xQ...",
      "token_type": "Bearer",
      "expires_in": 900
    }
  },
  "trace_id": "..."
}
```

失败：

| HTTP | code  | 场景                         |
| ---- | ----- | -------------------------- |
| 400  | 11400 | 验证码错误 / 已过期 / 试错超 5 次      |
| 409  | 11409 | 该邮箱已被注册                    |
| 422  | 10422 | 字段校验失败（见 `data.errors`）    |
| 429  | 11429 | 操作过于频繁                     |
| 502  | 21502 | 验证码邮件发送失败                  |
| 503  | 21503 | 鉴权功能未配置（后端没配 `JWT_SECRET`） |

> 注意顺序：服务端**先校验验证码，再检查邮箱是否重复**。所以"邮箱已注册"这个信息只有在验证码正确时才会返回 —— 这是刻意的，否则发码接口就成了账号枚举工具。

### 2.3 登录

`POST /auth/login`

```json
{ "email": "you@qq.com", "password": "你的密码" }
```

成功（200）与注册同结构（`AuthResult`）。

**密码错误和邮箱不存在返回完全相同的响应**（401 / code 11401 / `"邮箱或密码不正确"`），前端别指望区分。

### 2.4 刷新令牌

`POST /auth/refresh`

```json
{ "refresh_token": "9tK3xQ..." }
```

成功返回**新的 `AuthResult`**，里面是**全新的一对**令牌。

⚠️ **旧 refresh_token 立刻失效（轮换）**。所以并发刷新会互相作废 —— 前端必须做单飞（single-flight），见第 6 节的客户端实现。

### 2.5 登出

`POST /auth/logout`

```json
{ "refresh_token": "9tK3xQ..." }
```

成功：`{"code":0,"message":"已登出","data":null,...}`。幂等，传一个不存在的 token 也返回成功。

⚠️ `access_token` 在剩余有效期（默认 15 分钟）内**仍然可用** —— JWT 自包含的固有代价。前端登出后要主动清掉本地令牌，别指望它立刻失效。

### 2.6 当前用户

`GET /auth/me`，需要 `Authorization: Bearer <access_token>`

成功（200）：`data` 就是 `UserProfile`。

未登录 / 令牌无效：

```json
{ "code": 11401, "message": "缺少访问令牌", "data": null, "trace_id": "..." }
```

`message` 有两种：`缺少访问令牌`（没带 header）和 `登录已失效，请重新登录`（令牌无效或过期）。**按 `code` 判断，不要匹配文案。**

---

## 3. 对话

### 3.1 状态

`GET /chat/status`

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "configured": true,
    "model": "deepseek-flash",
    "base_url": "https://api.deepseek.com/v1",
    "prompt_id": "chat_system",
    "prompt_version": 1
  },
  "trace_id": "..."
}
```

`configured: false` 说明后端没配 `LLM_API_KEY`，此时两个对话接口都会返回 503。前端可以用它决定是否禁用输入框。

### 3.2 一次性对话

`POST /chat` —— **必须带 `Authorization: Bearer <access_token>`**。

```json
{
  "message": "用一句话介绍 LangChain",
  "conversation_id": "373820569239420928"
}
```

只有 `message` 必填：

| 字段                | 必填 | 作用                                                      |
| ----------------- | -- | ------------------------------------------------------- |
| `message`         | ✅  | 本轮提问，1–8000 字                                          |
| `conversation_id` | —  | 接着哪个会话聊。不传 = 新建会话，传 = 加载该会话最近 20 条历史后继续（省略即新建）        |

请求体里**没有** `history`。历史只从数据库读，客户端多传会被忽略 —— 详见本节末尾。

成功（200）：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "answer": "LangChain 是一个 LLM 应用开发框架。",
    "model": "deepseek-flash",
    "usage": { "prompt_tokens": 159, "completion_tokens": 42 },
    "conversation_id": "373820569239420928",
    "message_id": "373820569285558273"
  },
  "trace_id": "daf37e29..."
}
```

**`conversation_id` / `message_id` 是续聊和评价的前提**，两者一定非 `null`：

- 想接着聊 → 下次请求把 `conversation_id` 原样带回来
- 想点赞/点踩 → 用 `message_id` 调本文 3.5 的评价接口

一轮对话固定落两条消息（用户那条 + 助手那条），会话列表的 `message_count` 也随之 +2。上游失败时助手那条标 `failed`，正文为空串——不是不落库，否则"列表说 2 条、实际只有 1 条"永远对不上。

**为什么不能由前端传历史**

这是**防提示注入**：让浏览器决定喂给模型什么上下文，等于允许它伪造几条 assistant 消息把模型带跑偏，而请求参数看起来完全合法，服务端日志里查不出异常。所以契约里直接没有这个字段——客户端硬塞会被 Pydantic 丢弃，不报错也不生效。

参数约束（不满足直接 422）：

| 字段                  | 约束                |
| ------------------- | ----------------- |
| `message`           | 1–8000 字          |
| `conversation_id`   | 纯数字的雪花 ID 字符串；非法形状 422 |

### 3.3 流式对话（SSE）

`POST /chat/stream` 开工，请求体与 `/chat` 完全相同，同样**必须带 `Authorization`**。

⚠️ **不能用 `EventSource`**。`EventSource` 只支持 GET、不能带自定义 header、不能带 body。这里是 POST + JSON body，必须用 `fetch` + `ReadableStream` 手动解析。续传接口虽然是 GET，但同样用不了（鉴权要带 header），原因见 3.3.2 ①。

响应头：`content-type: text/event-stream; charset=utf-8`

**真实帧**（`\r\n` 分隔，这里为了可读按行展示）：

```
event: start
data: {"conversation_id":"373820601325846528","message_id":"373820601342623745"}

id: 1793194654751-0
event: delta
data: {"delta":"你好"}

id: 1793194654769-0
event: delta
data: {"delta":"，很高兴"}

id: 1793194654802-0
event: meta
data: {"model":"deepseek-flash","prompt_tokens":159,"completion_tokens":2,"conversation_id":"373820601325846528","message_id":"373820601342623745"}

id: 1793194654803-0
event: done
data: [DONE]
```

| event   | 带 `id:` | data | 说明 |
| ------- | ---- | --- | --- |
| `start` | ❌   | `{"conversation_id","message_id"}` | 首帧，**先拿到 ID 再说话**；续传和评价都要用。只在从头重放时出现（见 3.3.1） |
| `delta` | ✅   | `{"delta": "增量文本"}` | 可能出现很多次，**按顺序拼接** |
| `meta`  | ✅   | `{"model","prompt_tokens","completion_tokens","conversation_id","message_id"}` | 结束前一次，供统计用量；token 数可能为 `null` |
| `done`  | ✅   | `[DONE]`（纯文本，不是 JSON） | 正常结束 |
| `error` | ✅   | `{"code": 22502, "message": "..."}` | 流中途出错，之后不再有帧 |

**`id:` 是断线续传的锚点**，客户端必须存下**最后一个**收到的 `id:`。`start` 帧故意不带 id：万一在收到第一块之前就断了，`Last-Event-ID` 为空，续传从头开始，正好是想要的语义。

三个容易踩的点：

1. **流开始前**的错误（未登录 401、参数不合法 422、LLM 未配置 503、会话不存在 404、触发成本闸门 429）仍然是普通 JSON，HTTP 状态码是真的 —— 要先判 `res.ok`。所以鉴权、归属校验、限流都放在流开始之前，前端能拿到准确的状态码。
2. **流开始后**的错误只能靠 `event: error` 帧传（响应头已发出，状态码改不了）。所以两种错误通道都要处理。
3. **回答在 `done` 之前就落库了**。客户端收到 `done` 立刻刷新会话列表不会出现"刚聊完但列表里没有"。

服务端会周期性发 `: ping` 注释行防代理空闲超时，**注释行没有 event/data，解析时直接忽略**。

### 3.3.1 断线续传

网络抖动、切后台、锁屏都可能把连接掐掉。**不要重发 `POST /chat/stream`** —— 那会新建一轮、再扣一次 token，用户还得从头再看一遍。正确做法是拿最后一个 `id:` 去续：

```
GET /chat/stream/{message_id}
Authorization: Bearer <access_token>
Last-Event-ID: 1793194654769-0
```

`message_id` 来自 `start` 帧（或 `meta` 帧）。`Last-Event-ID` 也可以用查询参数 `?last_event_id=1793194654769-0`，效果相同。

| 场景 | 行为 |
| --- | --- |
| 生成还没结束 | 先补发断线期间产生的块，然后接着实时推 —— 用户看到的是"卡一下又继续"，不是重来 |
| 生成已结束 | 一次把剩下的块补完，补 `meta` + `done` 后关闭 |
| 不带 `Last-Event-ID` | 从第一块开始重放（刷新页面"接着看"用这条），此时才有 `start` 帧 |
| `Last-Event-ID` 已经在末尾之后 | 只回 `done`，立刻结束，不会挂着等 |
| 缓冲过期（默认留 10 分钟） | `410 / 22410`，改调本文 3.4 拉消息全文 |
| 消息不存在 / 不是自己的 | `404 / 10404`（两者同一个响应，不泄露存在性） |

**后端已经替你保住了生成**：连接断了之后生成还会继续跑，所以续传才能接上——但也因此有代价：你走之后它会继续烧 token。所以有 `SSE_RESUME_GRACE_SECONDS`（默认 30 秒）：**所有**客户端断开超过这个时间就停止生成，已产出的部分存为 `interrupted`，用户回来能看到半截回答而不是空白。窗口内重连则无缝接上。

前端要做的就是两件事：

```ts
let lastEventId: string | null = null;
let messageId: string | null = null;

// 1. 解析帧时记下 id（`id:` 行出现就更新）
function handleFrame(frame: { id?: string; event?: string; data?: string }) {
  if (frame.id) lastEventId = frame.id;
  if (frame.event === "start") messageId = JSON.parse(frame.data!).message_id;
  // ... delta / meta / done / error 各自处理
}

// 2. 断了就续，不要重发 POST
async function reconnect() {
  if (!messageId) return; // 连 start 都没收到，只能整轮重来
  const res = await fetch(`/api/v1/chat/stream/${messageId}`, {
    headers: {
      Authorization: `Bearer ${token}`,
      ...(lastEventId ? { "Last-Event-ID": lastEventId } : {}),
    },
  });
  // 之后和首连一样读流：补发的块会和已渲染的内容自然衔接
}
```

重连本身也要退避：`1s → 2s → 4s`，最多试 3 次。一直连不上就提示用户"网络不太好"，别无声地转圈。

### 3.3.2 续传落地清单（前端必读）

**① 续传是 GET，但照样用不了原生 `EventSource`**

续传接口是 GET，看起来可以用 `EventSource` 接。不行：`EventSource` 构造函数只接受 URL，**无法设置请求头**，而这里鉴权靠 `Authorization: Bearer`。浏览器那套"自动重连 + 自动带 `Last-Event-ID`"因此全部用不上。

唯一可行路径是 `fetch` + `ReadableStream` 手工解析（见第 7 节客户端实现）。将来若改成 Cookie 鉴权，`EventSource` 才能直接用。

**② 游标只存不解析**

`id:` 是服务端分配的 Redis Stream entry id，客户端**不要解析、不要自增、不要比较大小**，只保存"最后一个"并原样回传。传一个不存在的 id 不会报错、不会串位，只是读到空。

**③ `start` 帧只在从头重放时出现，但仍要写幂等**

带 `Last-Event-ID` 续读时后端**不再重发** `start`：客户端能拿出游标，就说明它收到过帧，会话/消息 ID 早已知道。只有"不带游标"或"收到 `start` 之后、第一块 `delta` 之前就断了"这两种从头重放的场景，才会再看到 `start`。

即便如此也别在 `start` 里做破坏性操作（比如清空已渲染文本）：用 `message_id` 判重，相同就继续渲染，不同才是新一轮。

**④ 五条终止路径，三条是"别再续了"**

| 收到什么 | 含义 | 前端该做什么 |
| --- | --- | --- |
| `event: done` | 正常结束 | 本地内容即最终版；刷新会话列表 |
| `event: error`，`code: 22408` | 生成已中止（长时间无人接收，或后端重启） | **停止重连**。半截内容在库里是 `interrupted`，改拉 `GET /conversations/{id}/messages` |
| `event: error`，其他 code | 上游或服务异常 | 停止重连，展示 `message`；已渲染的半截内容可保留 |
| HTTP `410`（`22410`） | 缓冲过期（默认 10 分钟） | **停止重连**，改拉历史消息全文 |
| HTTP `404` | 消息不存在或不属于当前用户 | 停止重连，回会话列表 |

`22408` 和 `410` 都意味着"再来也没用"，重试是纯粹的浪费。只有网络层失败（fetch 抛错、流异常中断）才值得退避重连。

**⑤ 重连必须有上限**

`1s → 2s → 4s`，最多 3 次。无限退避没有意义：缓冲有 TTL，等久了只会拿到 `410`。

**⑥ 断线不等于取消生成，也没有取消接口**

`AbortController.abort()`、切路由、关标签页都只是断开连接，后端生产者照常运行（续传能接上正是因为这个）。所以：

- 前端**不需要**"取消生成"接口，也没有提供；
- 想真的让它停：不管它，超过 grace 窗口（默认 30s）后端自己停；
- 这不是泄漏——`interrupted` 状态和已生成的内容都会落库。

**⑦ 同一轮支持多标签页同时读**

Redis Stream 不是消费队列，读完不删。两个标签页拿同一个 `message_id` 各自续读互不干扰，不需要加锁，也不需要做"全局只许一个连接"的单例。

**⑧ 两个后端边界（前端按 ④ 降级即可，不必额外处理）**

- 单轮缓冲最多 10000 片（`XADD MAXLEN`，防跑飞的护栏，不是容量规划）。真被截断时"从头重放"会缺开头几帧，正常按 id 续读不受影响。
- Redis 重启或 key 被淘汰 → 续传直接 `410`，与"过期"同样处理。

**⑨ 429 是"等一会儿"，不是"重试一下"**

`/chat`、`/chat/stream`、`/chat/regenerate` 都有成本闸门（每用户每分钟轮次、同时在途生成数、每日 token 额度），超限返回 `429 / 22429`，`message` 会说明是哪一种。**不要自动重试** —— 重试只会继续撞同一个计数。按 `message` 提示用户（"太快了，稍等几秒" / "同时只能进行 2 条对话" / "今日额度已用完"）。

顺带一个产品上的建议：前端应当在拿到 429 时**禁用发送按钮若干秒**，而不是让用户继续点 —— 服务端拦得住，但体验上一直在报错。

### 3.3.3 重新生成一条回答

`POST /chat/regenerate/{message_id}`，`message_id` 是**助手消息**。返回的帧格式、续传方式与 `/chat/stream` 完全一致（首帧 `start` 给新的 `message_id`，之后 `delta` / `meta` / `done`）。

```bash
curl -N -X POST http://127.0.0.1:8000/api/v1/chat/regenerate/373820569285558273 \
  -H "Authorization: Bearer <access_token>"
```

语义上有三点必须知道：

1. **提问不会被重复写入。** 会话里仍然是"一个问题 + 两条回答"，不是"两个问题 + 两条回答"。所以消息列表会出现两个 `role=assistant` 相邻的情况。
2. **旧回答保留**，新回答的 `parent_message_id` 指向它。前端据此把旧回答标成"已被重新生成"（比如折叠、置灰），而不是当成两轮对话渲染。消息列表接口已经返回这个字段。
3. **后续上下文只用新回答。** 之后继续聊，服务端会自动把被替代的那条排除，模型不会看到"问一次答两次"。

要点：这是一次**完整的重新推理**，token 成本与正常一轮相同，同样走成本闸门；被限流时返回 `429 / 22429`，不会在会话里留下空消息。

### 3.4 会话列表与历史

三个接口都要带 `Authorization`，且**只能看自己的**会话：别人的会话和不存在的会话返回同一个 `404`（区分了就能拿来探测某个 ID 是否存在）。

`GET /conversations?limit=20&cursor=<上一页的 next_cursor>`

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "id": "373820569239420928",
        "title": "用一句话说明什么是向量数据库",
        "message_count": 2,
        "model": "deepseek-flash",
        "last_message_at": "2026-10-28T13:08:51.204000",
        "created_at": "2026-10-28T13:08:51.204000"
      }
    ],
    "next_cursor": null
  },
  "trace_id": "..."
}
```

| 字段                | 说明                                        |
| ----------------- | ----------------------------------------- |
| `title`           | 首轮提问自动生成（前 30 字），可以改名                      |
| `message_count`   | 用户 + 助手消息总条数（冗余计数，列表页不必再查库里有多少条）           |
| `next_cursor`     | 为 `null` 表示没有下一页；否则原样回传给 `cursor` 继续翻      |

排序是**最近活跃优先**（`last_message_at` 倒序），不是创建时间 —— 三天前建的会话今天续聊，应该跳到列表最前面。

> 分页用游标而不是 `page=2`：会话的排序键会随聊天不断变化，用 offset 翻页必然漏行或重复行。

`GET /conversations/{id}/messages?limit=50&before_seq=<上一页的 next_before_seq>`

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "items": [
      {
        "id": "373820569269420032",
        "seq": 1,
        "role": "user",
        "content": "用一句话说明什么是向量数据库",
        "status": "complete",
        "model": null,
        "feedback": null,
        "feedback_reason": null,
        "feedback_at": null,
        "created_at": "2026-10-28T13:08:51.204000"
      },
      {
        "id": "373820569285558273",
        "seq": 2,
        "role": "assistant",
        "content": "向量数据库是专门存储和检索向量（高维数值）的数据库……",
        "status": "complete",
        "model": "deepseek-flash",
        "feedback": "up",
        "feedback_reason": null,
        "feedback_at": "2026-10-28T13:09:30.303246",
        "created_at": "2026-10-28T13:08:51.210000"
      }
    ],
    "next_before_seq": null
  },
  "trace_id": "..."
}
```

- `items` 按 `seq` **升序**，前端直接从上往下渲染，不用再排一遍
- 排序以 `seq` 为准，不是 `created_at`：同一毫秒可能插入两条，时间戳的顺序不确定
- `status` 有四种：`streaming`（正在生成）、`complete`、`interrupted`（用户中途断开，`content` 是已生成的部分）、`failed`（上游报错）
- `feedback` 是**当前登录用户**的评价：`up` / `down` / `null`

`PATCH /conversations/{id}` 改名，body `{"title": "新标题"}`（1–100 字）。

`DELETE /conversations/{id}` 删除，返回 `{"code": 0, "message": "已删除"}`。**软删除**：会话从列表消失，消息记录保留（排查"模型答歪了"要用）。重复删除同样返回 200，前端连点两下不会弹错误。

### 3.5 评价一条回复

`POST /messages/{message_id}/feedback`

```json
{ "rating": "down", "reason": "答的不是我问的" }
```

`rating` 三个取值，覆盖"赞 / 踩 / 取消"：

| `rating` | 含义   |
| -------- | ---- |
| `"up"`   | 赞    |
| `"down"` | 踩    |
| `null`   | 取消之前的评价（再点一次同一个按钮） |

`reason` 可选，最多 255 字。点踩时让用户选个原因，是最便宜的 badcase 采集方式。

响应：

```json
{
  "code": 0,
  "message": "已记录，感谢反馈",
  "data": {
    "message_id": "373820569285558273",
    "rating": "down",
    "feedback_reason": "答的不是我问的",
    "feedback_at": "2026-10-28T13:09:30.303246"
  },
  "trace_id": "..."
}
```

规则：

- **只能评价 assistant 消息**。给自己的提问点赞没有语义，会污染样本 → `400 / 10400`
- 别人的消息 → `404`（和"不存在"同一个响应）
- 重复提交会覆盖，不报错；`rating: null` 时 `feedback_reason` / `feedback_at` 一起清空
- 评价会立刻出现在本文 3.4 的消息列表里，前端不必自己维护本地状态

改动前的消息 `feedback` 是 `null`，不是 `"none"` —— 前端判断"有没有评价过"用 `feedback != null`，别用真假值（`"down"` 也是真值，但 `null` 才是没评价）。

---

## 4. 健康检查

| 接口                  | 成功响应 `data`                                                     |
| ------------------- | --------------------------------------------------------------- |
| `GET /health`       | `{"status":"ok","name":"learn-fastapi","version":"0.1.0"}`  |
| `GET /health/ready` | `{"status":"ready","checks":{"db":"ok","redis":"ok","llm":"ok"}}` |

`status` 三档含义：
- `ready`（HTTP 200）：全部正常
- `degraded`（HTTP 200）：有依赖未配置（如本地开发没起 Redis），服务仍可用
- `not_ready`（HTTP 503）：有依赖配了却连不上（如 MySQL 挂了），负载均衡应摘掉本实例

---

## 5. 错误码表

编码规则是 5 位：`X M SSS` = 归属 / 模块 / HTTP 状态码。

- **首位 `1`** = 调用方问题 → 提示用户改输入
- **首位 `2`** → 服务端或依赖问题 → 提示"稍后重试"，别让用户改

| code  | HTTP | 含义               | 前端建议                    |
| ----- | ---- | ---------------- | ----------------------- |
| 0     | 200  | 成功               | —                       |
| 10400 | 400  | 请求参数不合法          | 提示用户；如"只能评价助手回复"       |
| 10404 | 404  | 资源不存在 / 不属于当前用户  | 会话与消息查不到都归到这里（不区分存在性）    |
| 10405 | 405  | 方法不支持            | 检查 method               |
| 10409 | 409  | 资源冲突             | 提示用户                    |
| 10422 | 422  | 参数校验失败           | 读 `data.errors[]` 定位到字段 |
| 10429 | 429  | 请求过于频繁           | 倒计时重试                   |
| 10500 | 500  | 服务内部异常           | 报障（带 trace_id）          |
| 10502 | 502  | 上游服务异常           | 稍后重试                    |
| 10503 | 503  | 服务暂时不可用          | 稍后重试                    |
| 11400 | 400  | 验证码不正确或已过期       | 提示用户重新获取                |
| 11401 | 401  | 未登录 / 密码错 / 令牌失效 | 跳登录页或触发刷新               |
| 11403 | 403  | 没有权限             | 提示无权限                   |
| 11409 | 409  | 该邮箱已被注册          | 提示去登录                   |
| 11429 | 429  | 操作过于频繁           | 用 `resend_after` 倒计时    |
| 21502 | 502  | 验证码邮件发送失败        | 提示稍后重试                  |
| 21503 | 503  | 鉴权功能未配置          | 联系后端                    |
| 22502 | 502  | LLM 上游服务异常       | 提示稍后重试                  |
| 22503 | 503  | LLM 未配置          | 禁用对话入口                  |
| 22504 | 504  | LLM 上游超时         | 提示稍后重试                  |
| 22429 | 429  | 触发对话成本闸门         | 按 `message` 提示（刷太快 / 并发太多 / 额度用完），别自动重试轰炸 |
| 22408 | 408  | 长时间无客户端接收，生成中止   | 拉消息列表取已生成的部分；断线了就别让它空转 |
| 22410 | 410  | 流式缓冲已过期         | 改调 `GET /conversations/{id}/messages` 拿全文 |

422 的 `data` 结构固定：

```json
{ "errors": [ { "field": "email", "message": "value is not a valid email address..." } ] }
```

`field` 是点分路径，可以直接映射到表单字段（如 `email`、`conversation_id`）。

---

## 6. TypeScript 类型定义

```ts
// api-types.ts

/** 统一响应外壳 */
export interface ApiEnvelope<T> {
  code: number;
  message: string;
  data: T | null;
  trace_id: string;
}

/** 422 时的 data */
export interface ValidationDetail {
  errors: Array<{ field: string; message: string }>;
}

export interface UserProfile {
  /** 雪花 ID，19 位字符串，禁止 Number() */
  id: string;
  email: string;
  nickname: string;
  /** ISO 8601，UTC 带 Z 后缀，可直接 new Date() */
  created_at: string;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: "Bearer";
  /** access_token 剩余有效秒数 */
  expires_in: number;
}

export interface AuthResult {
  user: UserProfile;
  token: TokenPair;
}

export interface SendCodeResult {
  email: string;
  expires_in: number;
  /** 多少秒后可以重发 */
  resend_after: number;
}

export interface ChatStatus {
  configured: boolean;
  model: string;
  base_url: string;
  prompt_id: string;
  prompt_version: number;
}

export interface ChatMessage {
  /** 只允许 user / assistant，system 会被拒 —— 这个类型只用于渲染历史，不进请求体 */
  role: "user" | "assistant";
  content: string;
}

export interface ChatRequest {
  message: string;
  /** 接着哪个会话聊。不传=新建会话，传=服务端加载该会话历史后继续 */
  conversation_id?: string;
  // 没有 history 字段：上下文只从服务端加载，传了也不会生效
}

export interface ChatResponse {
  answer: string;
  model: string;
  usage: Record<string, number> | null;
  conversation_id: string;
  /** 评价接口要用的就是它 */
  message_id: string;
}

/**
 * SSE 事件（客户端解析后的形状）。
 * 除 start 外都带 `id`：它是续传的锚点，调用方要存下最后一个。
 */
export type ChatStreamEvent =
  | { type: "start"; conversation_id: string; message_id: string }
  | { type: "delta"; delta: string; id?: string }
  | {
      type: "meta";
      model: string;
      prompt_tokens: number | null;
      completion_tokens: number | null;
      conversation_id: string;
      message_id: string;
      id?: string;
    }
  | { type: "done"; id?: string }
  | { type: "error"; code: number; message: string; id?: string };

// ---------------------------------------------------------------- 会话与评价

export type FeedbackRating = "up" | "down";

export interface ConversationSummary {
  /** 雪花 ID，**字符串**，别 Number() 转 */
  id: string;
  title: string;
  message_count: number;
  model: string;
  last_message_at: string;
  created_at: string;
}

export interface ConversationListResponse {
  items: ConversationSummary[];
  /** 原样回传给下一页；null 表示没有更多 */
  next_cursor: string | null;
}

export interface MessageItem {
  id: string;
  /** 会话内序号，排序以它为准 */
  seq: number;
  role: "user" | "assistant" | "system" | "tool";
  /** 流式中断时是已生成的部分，不会是 null */
  content: string;
  status: "streaming" | "complete" | "interrupted" | "failed";
  model: string | null;
  /**
   * 非空 = 这条回答是重新生成的，值是被它替代的那条消息 ID。
   * 前端应把旧回答标成"已被重新生成"，而不是当成两轮对话渲染。
   */
  parent_message_id: string | null;
  /** 当前登录用户的评价；null = 没评价过 */
  feedback: FeedbackRating | null;
  feedback_reason: string | null;
  feedback_at: string | null;
  created_at: string;
}

export interface MessageListResponse {
  items: MessageItem[];
  /** 传回 before_seq 继续往前翻；null 表示已到最早 */
  next_before_seq: number | null;
}

export interface FeedbackResponse {
  message_id: string;
  rating: FeedbackRating | null;
  feedback_reason: string | null;
  feedback_at: string | null;
}
```

---


## 7. 可直接用的客户端

`fetch` 封装 + 401 单飞刷新 + SSE 解析。复制即用，无第三方依赖。

结构按依赖顺序：`ApiError`（错误类型）→ `auth`（令牌存储）→ `request`（含 401 重放）→ `refreshOnce`（单飞）→ `parseSSE`（流解析）→ `api`（对外方法）。

```ts
// api-client.ts
import type {
  ApiEnvelope,
  AuthResult,
  ChatRequest,
  ChatResponse,
  ChatStreamEvent,
  ChatStatus,
  SendCodeResult,
  TokenPair,
  UserProfile,
  ValidationDetail,
} from "./api-types";

const BASE = "http://127.0.0.1:8000/api/v1";

/** 业务错误：HTTP 状态码与业务码都保留，方便上层分流处理 */
export class ApiError extends Error {
  constructor(
    readonly code: number,
    message: string,
    readonly status: number,
    readonly traceId = "",
    readonly data: unknown = null,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** 422：把 errors 转成 { 字段: 提示 }，直接喂给表单 */
  get fieldErrors(): Record<string, string> {
    const errors = (this.data as ValidationDetail | null)?.errors ?? [];
    // as const 不能省：不加的话 map 推断出 string[][]，
    // 而 Object.fromEntries 要求 Iterable<[PropertyKey, T]>
    return Object.fromEntries(errors.map((e) => [e.field, e.message] as const));
  }
}

// ---------------------------------------------------------------- 令牌存储

const STORAGE_KEY = "auth";

export interface StoredAuth {
  access: string;
  refresh: string;
  /** 毫秒时间戳，用于提前判断是否需要刷新 */
  expiresAt: number;
}

export const auth = {
  get(): StoredAuth | null {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as StoredAuth) : null;
  },
  set(pair: TokenPair) {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        access: pair.access_token,
        refresh: pair.refresh_token,
        expiresAt: Date.now() + pair.expires_in * 1000,
      } satisfies StoredAuth),
    );
  },
  clear() {
    localStorage.removeItem(STORAGE_KEY);
  },
  /** 提前 30 秒视为过期，避免请求途中刚好失效 */
  isExpiringSoon(): boolean {
    const current = auth.get();
    return !current || current.expiresAt - Date.now() < 30_000;
  },
};

// ---------------------------------------------------------------- 底层请求

async function toApiError(res: Response): Promise<ApiError> {
  // 网关/代理层的错误可能不是 JSON，兜底成通用错误
  try {
    const body = (await res.json()) as ApiEnvelope<unknown>;
    return new ApiError(body.code, body.message, res.status, body.trace_id, body.data);
  } catch {
    return new ApiError(-1, `请求失败（HTTP ${res.status}）`, res.status);
  }
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  /** 内部标记：已经重试过一次，避免刷新死循环 */
  _retried?: boolean;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, _retried = false } = options;
  const current = auth.get();

  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(current ? { Authorization: `Bearer ${current.access}` } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  // 401 且不是刷新接口本身：刷新一次再重放
  if (res.status === 401 && !_retried && current && !path.startsWith("/auth/refresh")) {
    try {
      await refreshOnce();
    } catch {
      auth.clear();
      throw await toApiError(res);
    }
    return request<T>(path, { ...options, _retried: true });
  }

  if (!res.ok) throw await toApiError(res);

  const envelope = (await res.json()) as ApiEnvelope<T>;
  if (envelope.code !== 0) {
    throw new ApiError(envelope.code, envelope.message, res.status, envelope.trace_id, envelope.data);
  }
  return envelope.data as T;
}

// ---------------------------------------------------------------- 单飞刷新

let refreshing: Promise<AuthResult> | null = null;

/**
 * 并发 401 时只发一次刷新请求。
 * refresh_token 是轮换的（旧的用完即废），并发刷新会互相作废，
 * 表现为"偶发被踢下线"——所以必须单飞。
 */
function refreshOnce(): Promise<AuthResult> {
  if (refreshing) return refreshing;

  const current = auth.get();
  const task = (async () => {
    if (!current) throw new ApiError(11401, "未登录", 401);
    const result = await request<AuthResult>("/auth/refresh", {
      method: "POST",
      body: { refresh_token: current.refresh },
      _retried: true,
    });
    auth.set(result.token);
    return result;
  })().finally(() => {
    refreshing = null;
  });

  refreshing = task;
  return task;
}

// ---------------------------------------------------------------- SSE 解析

interface SSEFrame {
  event: string;
  data: string;
  /** 续传锚点：客户端要存下最后一个，重连时作为 Last-Event-ID 回传 */
  id?: string;
}

/**
 * 把 SSE 字节流解析成事件。
 * 兼容 \r\n / \n，忽略 `:` 开头的注释行（服务端的 ping 心跳）。
 */
async function* parseSSE(stream: ReadableStream<Uint8Array>): AsyncGenerator<SSEFrame> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let event = "";
  let eventId = "";
  let dataLines: string[] = [];

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");

      const lines = buffer.split("\n");
      // 最后一段可能是半行，留到下一轮
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        if (line === "") {
          // 空行 = 一个事件结束
          if (dataLines.length > 0) {
            yield { event: event || "message", data: dataLines.join("\n"), id: eventId || undefined };
          }
          event = "";
          eventId = "";
          dataLines = [];
          continue;
        }
        if (line.startsWith(":")) continue; // 注释行 / ping

        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        let fieldValue = colon === -1 ? "" : line.slice(colon + 1);
        if (fieldValue.startsWith(" ")) fieldValue = fieldValue.slice(1);

        if (field === "event") event = fieldValue;
        else if (field === "id") eventId = fieldValue;
        else if (field === "data") dataLines.push(fieldValue);
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/**
 * SSE 帧 → 业务事件。
 * 首连和续传共用同一套映射：续传拿到的是"剩下的帧"，形状完全一样。
 */
async function* chatEvents(stream: ReadableStream<Uint8Array>): AsyncGenerator<ChatStreamEvent> {
  for await (const frame of parseSSE(stream)) {
    if (frame.event === "start") {
      const ids = JSON.parse(frame.data) as { conversation_id: string; message_id: string };
      yield { type: "start", ...ids };
    } else if (frame.event === "delta") {
      const chunk = JSON.parse(frame.data) as { delta: string };
      yield { type: "delta", delta: chunk.delta, id: frame.id };
    } else if (frame.event === "meta") {
      const meta = JSON.parse(frame.data) as {
        model: string;
        prompt_tokens: number | null;
        completion_tokens: number | null;
        conversation_id: string;
        message_id: string;
      };
      yield { type: "meta", ...meta, id: frame.id };
    } else if (frame.event === "done") {
      yield { type: "done", id: frame.id };
      return;
    } else if (frame.event === "error") {
      const err = JSON.parse(frame.data) as { code: number; message: string };
      yield { type: "error", code: err.code, message: err.message, id: frame.id };
      return;
    }
  }
}

// ---------------------------------------------------------------- 对外接口

export const api = {
  // ---- 鉴权 ----
  // captcha: VAPTCHA validate() 的四件套 { token, knock, dfu?, ip? }，
  // 服务端启用了人机验证时必传。前端不要探测后端开没开 —— 默认就当必传，
  // 后端没启用时这几个字段会被忽略，传了不报错（见 2.1.1）
  sendCode(email: string, captcha?: { token: string; knock: string; dfu?: string; ip?: string }) {
    return request<SendCodeResult>("/auth/verification-code", {
      method: "POST",
      body: { email, scene: "register", ...captcha },
    });
  },

  register(input: { email: string; password: string; code: string; nickname?: string }) {
    return request<AuthResult>("/auth/register", { method: "POST", body: input }).then((r) => {
      auth.set(r.token);
      return r;
    });
  },

  login(email: string, password: string) {
    return request<AuthResult>("/auth/login", {
      method: "POST",
      body: { email, password },
    }).then((r) => {
      auth.set(r.token);
      return r;
    });
  },

  async logout() {
    const current = auth.get();
    try {
      if (current) {
        await request<null>("/auth/logout", {
          method: "POST",
          body: { refresh_token: current.refresh },
        });
      }
    } finally {
      auth.clear(); // 无论服务端是否成功，本地令牌都要清
    }
  },

  me() {
    return request<UserProfile>("/auth/me");
  },

  // ---- 对话 ----
  chatStatus() {
    return request<ChatStatus>("/chat/status");
  },

  chat(payload: ChatRequest) {
    return request<ChatResponse>("/chat", { method: "POST", body: payload });
  },

  /**
   * 流式对话。用 async generator 暴露事件，调用方 for await 即可。
   * 注意：不能用 EventSource —— 这里是 POST + JSON body。
   * 断线后**不要重发本方法**，改用 resumeChatStream（见 3.3.1、3.3.2）。
   */
  async *chatStream(payload: ChatRequest, signal?: AbortSignal): AsyncGenerator<ChatStreamEvent> {
    const current = auth.get();
    const res = await fetch(`${BASE}/chat/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(current ? { Authorization: `Bearer ${current.access}` } : {}),
      },
      body: JSON.stringify(payload),
      signal,
    });

    // 流开始前的错误（401 / 422 / 429 / 503）仍是普通 JSON
    if (!res.ok) throw await toApiError(res);
    if (!res.body) throw new ApiError(-1, "响应体为空", res.status);
    yield* chatEvents(res.body);
  },

  /**
   * 重新生成一条回答（见 3.3.3）。
   * 与 chatStream 的区别只有 URL：帧格式、错误通道、续传方式完全一样。
   * 新回答是一条**新消息**，旧回答保留并被标记 parent_message_id。
   * 429 / 22429 = 触发成本闸门，别自动重试。
   */
  async *regenerateStream(
    messageId: string,
    signal?: AbortSignal,
  ): AsyncGenerator<ChatStreamEvent> {
    const current = auth.get();
    const res = await fetch(`${BASE}/chat/regenerate/${messageId}`, {
      method: "POST",
      headers: current ? { Authorization: `Bearer ${current.access}` } : {},
      signal,
    });
    if (!res.ok) throw await toApiError(res);
    if (!res.body) throw new ApiError(-1, "响应体为空", res.status);
    yield* chatEvents(res.body);
  },

  /**
   * 断线续传：从 lastEventId 之后接着读，不重新生成、不重复计费。
   * messageId 来自 start / meta 帧；lastEventId 取最后一个事件的 id，
   * 不传就是从头重放（刷新页面"接着看"用）。
   * 410（缓冲过期）→ 改调 listMessages 拿全文。
   * 流内 error 22408（生成已中止）→ 同样停止重连，改拉 listMessages。
   * 重连退避 1s→2s→4s，最多 3 次；这两类错误不要重试。
   */
  async *resumeChatStream(
    messageId: string,
    lastEventId?: string,
    signal?: AbortSignal,
  ): AsyncGenerator<ChatStreamEvent> {
    const current = auth.get();
    const res = await fetch(`${BASE}/chat/stream/${messageId}`, {
      headers: {
        ...(current ? { Authorization: `Bearer ${current.access}` } : {}),
        ...(lastEventId ? { "Last-Event-ID": lastEventId } : {}),
      },
      signal,
    });

    if (!res.ok) throw await toApiError(res);
    if (!res.body) throw new ApiError(-1, "响应体为空", res.status);
    yield* chatEvents(res.body);
  },

  // ---- 会话（都要登录）----
  listConversations(params: { limit?: number; cursor?: string } = {}) {
    const qs = new URLSearchParams();
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.cursor) qs.set("cursor", params.cursor);
    const suffix = qs.toString();
    return request<ConversationListResponse>(`/conversations${suffix ? `?${suffix}` : ""}`);
  },

  getConversation(id: string) {
    return request<ConversationSummary>(`/conversations/${id}`);
  },

  /** 打开会话时取最后 N 条；往上滚时把 next_before_seq 作为 before_seq 再取 */
  listMessages(id: string, params: { limit?: number; before_seq?: number } = {}) {
    const qs = new URLSearchParams();
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.before_seq) qs.set("before_seq", String(params.before_seq));
    const suffix = qs.toString();
    return request<MessageListResponse>(`/conversations/${id}/messages${suffix ? `?${suffix}` : ""}`);
  },

  renameConversation(id: string, title: string) {
    return request<ConversationSummary>(`/conversations/${id}`, {
      method: "PATCH",
      body: { title },
    });
  },

  deleteConversation(id: string) {
    return request<null>(`/conversations/${id}`, { method: "DELETE" });
  },

  /** 赞 / 踩 / 取消（rating: null）。只能评 assistant 消息 */
  setFeedback(messageId: string, rating: FeedbackRating | null, reason?: string) {
    return request<FeedbackResponse>(`/messages/${messageId}/feedback`, {
      method: "POST",
      body: { rating, reason },
    });
  },
};
```

### 用法示例

> 下面的 `render` / `toast` 是占位函数，换成你项目里的渲染与提示。

```ts
// 登录
await api.login("you@qq.com", "你的密码");

// 一次性对话（必须先登录：没令牌返回 401 / 11401）
const { answer, conversation_id, message_id } = await api.chat({ message: "你好" });

// 继续这个会话：只带 conversation_id，历史由服务端加载
await api.chat({ message: "展开讲讲", conversation_id });

// 流式对话：记住 messageId 和最后一个 id，断线才有得续
let text = "";
let messageId: string | null = null;
let lastEventId: string | undefined;

for await (const ev of api.chatStream({ message: "讲个笑话" })) {
  if (ev.type === "start") {
    messageId = ev.message_id; // 首帧就给，别等 meta
  } else if (ev.type === "delta") {
    text += ev.delta;
    lastEventId = ev.id; // 每个 id 都存，续传从它之后开始
    render(text); // 逐字渲染
  } else if (ev.type === "meta") {
    console.log("用量", ev.prompt_tokens, ev.completion_tokens);
  } else if (ev.type === "error") {
    toast(ev.message); // 流中途出错
  }
}

// 断了（网络抖动 / 切后台）：续，不要重发 chatStream
async function resume() {
  if (!messageId) return; // start 都没收到，只能整轮重来
  for await (const ev of api.resumeChatStream(messageId, lastEventId)) {
    if (ev.type === "delta") {
      text += ev.delta;
      render(text); // 和已经渲染的内容自然衔接，不会重影
    }
  }
}

// 用户点"停止"：abort 只是断开连接，服务端还会继续生成
// （默认 30 秒内没人回来才停），所以再次进入会话能直接读到完整回答
const controller = new AbortController();
const task = api.chatStream({ message: "写一篇长文" }, controller.signal);
// controller.abort()
```

### 表单错误映射

```ts
try {
  await api.register({ email, password, code });
} catch (e) {
  if (e instanceof ApiError && e.code === 10422) {
    setFieldErrors(e.fieldErrors); // { email: "...", password: "..." }
  } else if (e instanceof ApiError && e.code === 11409) {
    showTip("该邮箱已注册，请直接登录");
  } else if (e instanceof ApiError && e.code === 11429) {
    startCountdown(60);
  } else {
    showTip((e as ApiError).message);
  }
}
```

---

## 8. 联调自测

```bash
# 存活
curl.exe http://127.0.0.1:8000/api/v1/health

# 对话是否可用（不打真网络）
curl.exe http://127.0.0.1:8000/api/v1/chat/status

# 未登录调对话：401 / 11401
curl.exe -X POST http://127.0.0.1:8000/api/v1/chat -H "Content-Type: application/json" -d "{\"message\":\"你好\"}"
```

带令牌：

```bash
curl.exe http://127.0.0.1:8000/api/v1/auth/me -H "Authorization: Bearer <access_token>"

# 流式（必须登录）
curl.exe -N -X POST http://127.0.0.1:8000/api/v1/chat/stream ^
  -H "Authorization: Bearer <access_token>" -H "Content-Type: application/json" ^
  -d "{\"message\":\"你好\"}"

# 断线续传：message_id 来自 start 帧，Last-Event-ID 是上次最后一个 id:
curl.exe -N "http://127.0.0.1:8000/api/v1/chat/stream/<message_id>" ^
  -H "Authorization: Bearer <access_token>" -H "Last-Event-ID: 1793194654769-0"
```

> 想手工验证"续传真的接上了"：先跑上面那条 POST 并在中途 `Ctrl+C`（连接断掉、生成不停），
> 再用最后看到的 `id:` 发第二条命令 —— 输出的文本应该正好接在断点之后，最后以 `meta` + `done` 收尾。

浏览器里还能直接用 Swagger：<http://127.0.0.1:8000/docs>（右上角 Authorize 填 `Bearer <token>`）。

### 8.1 本地联调页（人机验证那一层）

`web/` 下有两个**零构建**的静态页，用来在真浏览器里走一遍链路 —— VAPTCHA 的验证窗口只能在浏览器里弹，curl 测不了这一段。

| 页面 | 用途 |
| --- | --- |
| `web/tmp-vaptcha-test.html` | 单点验证：SDK 能否加载 → 能否初始化 → `validate()` 能否拿到四件套。后端没起也能用 |
| `web/vaptcha-demo.html` | 全链路：人机验证 → 发码 → 注册 → 登录 → `/auth/me`，右侧带请求日志与错误码解释 |

**必须用 HTTPS 打开，不能用 `http://127.0.0.1:5173`。** 这是 VAPTCHA 的硬要求：http 页面调 `v4c.vaptcha.com/api/config` 一律返回 `code:9 验证单元不匹配`，SDK 初始化直接失败（报 `当前网站未授权`），跟 VID、白名单都无关。

起服务：

```bash
# 后端（HTTP 即可）
python -m uvicorn app.main:app --port 8000

# 页面：HTTPS + 同源反代 /api -> 127.0.0.1:8000
python .tmp-tls/serve_https.py
```

打开 <https://127.0.0.1:5443/vaptcha-demo.html>（自签证书，浏览器点"继续前往"）。

`.tmp-tls/serve_https.py` 做三件事，都是为了绕开"HTTPS 页面不能 fetch HTTP 后端"这条：

1. 用自签证书把 `web/` 目录以 HTTPS 提供出去（端口 5443）；
2. 把 `/api/*` 反向代理到 `127.0.0.1:8000` —— 页面同源调用，既没有混合内容，也不用改后端 `CORS_ORIGINS`；
3. 页面检测到自身是 https 时，API Base 自动取 `location.origin + "/api/v1"`。

**自签证书必须导入信任，否则验证窗口白屏**

只点「继续前往」是不够的：证书报错的页面在 Chrome 里**不是安全上下文**，`window.isSecureContext === false`，`crypto.subtle` 直接是 `undefined`。而验证窗口里的 `pow.js` 用 `crypto.subtle.digest` 算工作量证明——算不出来就停在 loading，表现就是**窗口弹出来了、里面全白**。

导入到当前用户级根证书（不需要管理员，2 天后随证书一起过期）：

```bash
cd .tmp-tls
openssl x509 -in cert.pem -outform DER -out cert.cer
certutil -addstore -user Root cert.cer   # 删除：certutil -delstore -user Root "localhost"
```

导入后**关掉整个浏览器再打开**（同一个标签页刷新可能还沿用旧的证书判定），页面顶部的「连接配置」应该显示 `安全上下文正常 / isSecureContext=是 / crypto.subtle 可用`。这条诊断是页面自带的，绿了再去点人机验证。

**Vue 前端用 `server.cjs` 时，别把 Referrer-Policy 设成 `no-referrer`**

`frontend/server.cjs` 起初给所有响应设了 `Referrer-Policy: no-referrer`，结果 VAPTCHA 的 config 接口（`https://v4c.vaptcha.com/api/config?v=1&d=...`）收不到 `Referer`，上游回 `code 9`，SDK 抛「当前网站未授权」，注册和发码一起废掉。

判定链条只有四步，按这个顺序查最快：

1. 页面响应头是 `Referrer-Policy: no-referrer`；
2. 于是 config 请求的 `Referer` 是空字符串（DevTools Network 里能看到）；
3. 上游返回 `{"code":9}` —— SDK 里 `9` 硬编码成「当前网站未授权」（`8` 是验证单元不存在，`10` 是未启用）；
4. 用 `curl` 带上 `Referer: https://127.0.0.1:4318/` 重放同一个 `d` 参数，会得到 `{"code":0,"msg":"success"}`，反证域名本身是通的。

改成 `strict-origin-when-cross-origin`（浏览器默认值，跨源只带 origin、不带 path 与 query）即可。**`vite dev` 没设这个头，只有 `node server.cjs` 会踩**，所以症状是"开发环境人机验证正常、build 之后必失败"，很容易误判成平台侧白名单问题。

**Edge 兼容：强制内嵌验证，不弹独立窗口**

SDK 在 PC 上默认走 `window.open` 弹一个验证窗口，由父页面用 `postMessage` 把初始化数据（boot）递进去。Edge 里这条通道不成立：窗口能打开、标题也渲染出来，但一直停在「验证准备中…」，既不画图也不报错——本质是那个窗口没收到 boot（单独打开 `verify.html` 也是同一现象，它的 `postMessage` 找不到 opener）。

两个联调页因此都加了一个十几行的兼容层：**把 SDK 的内嵌分支固定为开启**，验证面片直接展开在当前页面里（SDK 自己挂的全屏遮罩层 `#vaptcha_iframe_overlay`，不占布局），Chrome / Edge 表现一致，也不再依赖 popup 里的 `window.opener`。

```js
function forceInlineVerify() {
  const proto = window.VaptchaCore && window.VaptchaCore.prototype;
  if (!proto || typeof proto.shouldUseIframe !== "function") return false; // SDK 改了就跳过
  proto.shouldUseIframe = function () { return true; };
  return true;
}
```

调用点：初始化之后、每次 `validate()` 之前各调一次（`core.js` 是懒加载的，早于初始化调用会拿不到 `VaptchaCore`）。

> 这是调试页里的兜底，不是官方 API。真实前端如果也用内嵌形态，优先在控制台把验证单元设成内嵌，或直接让 SDK 走默认弹窗——**只有确认弹窗分支在你的目标浏览器里可用时才用默认**。

页面报错时先看它自己打的日志：

| 现象 | 含义 |
| --- | --- |
| `当前网站未授权` + 页面是 http | 换 HTTPS，见上 |
| `当前域名未授权`，已是 HTTPS | 白名单没放行该 hostname（`127.0.0.1` / `localhost` 要各加一条） |
| 验证窗口停在「验证准备中…」 | 那个窗口没拿到父页面的 boot（Edge 走弹窗就是这条），改用内嵌，见上 |
| `fetch 抛错` | 后端没起、端口不对，或 CORS 没放行页面来源 |
| `HTTP 400 / code 11400` | 票据缺失或验签没过（看 message 区分） |
| `HTTP 504 / code 21504` | 验签上游不可达，fail-closed |

---

## 9. 还没做的（前端先别按有来做）

| 项        | 现 状                                        |
| -------- | ------------------------------------------ |
| ~~对话接口鉴权~~ | ✅ 已做：`/chat`、`/chat/stream` **强制登录**，没带令牌 `401 / 11401`；`user_id` 从令牌取，归属写进 WHERE |
| ~~对话历史持久化~~ | ✅ 已做：会话列表 + 历史消息 + 服务端加载上下文，见 3.4 |
| ~~对话评价~~ | ✅ 已做：赞 / 踩 / 取消写进 `messages.feedback`，见 3.5 |
| ~~SSE 断线续传~~ | ✅ 已做：每帧带 `id:`，用 `GET /chat/stream/{message_id}` + `Last-Event-ID` 续读，见 3.3.1；前端落地清单见 3.3.2 |
| ~~重新生成回答~~ | ✅ 已做：`POST /chat/regenerate/{message_id}`（SSE），旧回答保留并用 `parent_message_id` 标记，见 3.3.3 |
| ~~成本闸门~~ | ✅ 已做：超限 `429 / 22429`，见 3.3.2 ⑨ |
| 会话搜索     | 没有搜索接口，列表只能按最近活跃翻页                          |
| 忘记密码     | 没有重置密码接口                                   |
| 人机验证     | ✅ 已接入，只保护 `POST /auth/verification-code`（注册/登录页共用发码，够用）；其他写接口没挂 |
