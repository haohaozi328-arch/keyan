# 启学聊天前端 v2.0 · Vue

Vue 3 + Vite 实现，使用单文件组件、Composition API 和响应式状态。保留豆包风格的侧栏、欢迎页和底部输入框，支持电脑和手机，直接对接仓库内的 FastAPI 后端。

## 从源码启动

建议 Node.js 24 LTS，也支持 Node.js 20.19+ 或 22.12+。先启动后端所需的 MySQL、Redis 和 FastAPI，默认后端地址为 `http://127.0.0.1:8000`。

在仓库的 `frontend` 目录打开 PowerShell，首次运行：

```powershell
npm ci --ignore-scripts --no-audit --no-fund
npm run dev
```

访问 `http://127.0.0.1:4318`，修改 Vue 文件会自动刷新。开发服务器和正式服务器使用同一套接口代理与公开配置。

正式构建和启动：

```powershell
npm run build
node .\server.cjs
```

也可运行 `npm start` 或双击 `start.cmd`：已安装依赖时先构建再启动。交付 ZIP 已包含 `dist`，解压后可直接双击 `start.cmd`，无需先安装 npm 依赖；修改源码仍需安装依赖并重新构建。不要直接双击 `index.html`，也不要把源码入口单独部署。

## 后端与验证码配置

在启动命令之前设置环境变量：

```powershell
$env:BACKEND_URL = 'http://127.0.0.1:8000'
$env:BACKEND_ENV_FILE = (Resolve-Path '..\.env').Path
npm start
```

`BACKEND_URL` 只填服务地址，不加 `/api/v1`。地址变更需重启前端。正式服务器端口可通过 `$env:FRONTEND_PORT = '4319'` 修改；开发端口在 `vite.config.mjs` 中修改。

`BACKEND_ENV_FILE` 指向后端已有配置；未指定时读取前端目录上一级的 `.env`。服务端只公开验证码 VID 和启用状态，VKEY、模型密钥不会发给浏览器。不要把密钥放入 `VITE_*` 变量，这类变量会被打包进网页。交付包不包含后端配置。

## 注册与 HTTPS

后端启用 VAPTCHA 时，发送注册验证码需要 HTTPS、有效的 VAPTCHA 配置和后端邮件服务。已有账号可在本机 HTTP 页面登录。使用浏览器信任且包含 `127.0.0.1` 的证书后，启动正式服务器：

```powershell
$env:TLS_CERT = 'C:\certs\localhost.pem'
$env:TLS_KEY = 'C:\certs\localhost-key.pem'
npm start
```

此时访问 `https://127.0.0.1:4318`。证书需自行准备并受信任，VAPTCHA 控制台需允许当前域名。

## 功能与接口

- 登录、邮箱注册、单次并发令牌刷新和退出。
- SSE 流式回答、停止接收、GET 断线续传，保留长 ID 字符串。
- 历史会话及消息分页、改名、删除、赞踩及取消。
- 安全 Markdown 与代码块、复制、导出 Markdown。
- 学习提问卡片、逐步提示开关、后端状态、手机侧栏。

接口路径沿用 `/api/v1/auth`、`/chat`、`/conversations`、`/messages` 和 `/health`，未更改后端契约。登录状态存于当前标签页的 sessionStorage；历史内容保存在后端。

“逐步提示”在请求中附加学习方式说明，效果取决于后端模型。“停止接收”中断本地读取，后端仍可能短暂生成；续传读取同一消息，不重新 POST 生成。续传过期后可重开历史查看保存内容。

当前没有文件上传、联网搜索、知识库或模型切换。服务仅监听本机；公网部署需要另行配置。

## 代码分工

| 文件 | 职责 |
| --- | --- |
| `src/App.vue` | 页面布局、键盘操作、滚动 |
| `src/components/ChatSidebar.vue` | 历史会话、账户入口 |
| `src/components/ChatComposer.vue` | 输入、提示模式、停止和续传 |
| `src/components/ChatMessage.vue` | 消息展示、复制、评价 |
| `src/components/AuthDialog.vue` | 登录注册表单、VAPTCHA |
| `src/components/MarkdownContent.vue` | 安全 Markdown 渲染边界 |
| `src/composables/useChat.js` | 响应式状态、聊天及账户流程 |
| `api-client.mjs` | HTTP、令牌刷新、SSE 解析 |
| `server.cjs` / `vite.config.mjs` | 正式静态服务 / 开发服务及共享代理 |

## 验证

```powershell
npm run check
npm test
npm run build
npm audit --registry=https://registry.npmjs.org
```

测试使用 Node 内置测试工具、Vitest、Vue Test Utils 和 jsdom，挂载真实 Vue 组件并模拟接口。测试不调用模型、不发邮件、不接触真实数据库。验证范围和真实联调清单见 [验证记录](验证记录.md)。
