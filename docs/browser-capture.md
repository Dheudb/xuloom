# 后台渲染与视觉验证

本机桌面可以使用自带 Chromium 在隐藏窗口中渲染工作区 HTML 或 HTTP(S) 页面，截图后直接将图像交给支持图片输入的模型。CLI 和 SSH 使用执行机器上的可选 Playwright/Chromium。用户不需要先打开文件预览或浏览器窗口。这是网页渲染截图，不是截取用户桌面或控制其他原生应用。

## 使用

直接要求“渲染 page.html，截图检查改动后的布局”。浏览器工具默认按需发现，通过 `tool_search_tool` 激活 `browser_tool`；没有增加自动截图的全局策略或额外模型规划回合。

```json
{"action":"render","file_path":"page.html","width":1440,"height":960,"wait_ms":500,"full_page":false}
```

开发服务器使用 `url`，例如 `http://127.0.0.1:3000`。SSH 聊天中的这个地址属于远端机器。`file_path` 和 `url` 二选一。文件限当前执行工作区内的 HTML，可加载工作区内非隐藏路径的 CSS、JavaScript、图片等资源。桌面和 CLI/SSH 的一次性渲染均不通过 `file://` 暴露磁盘。要验证 React/Vite 等项目，先通过现有命令工具在相同执行机器启动其开发服务，再渲染相应地址；工具不会自动猜测构建命令。

可选视口：宽 320–2560、高 240–2160 CSS 像素；等待 0–10000 毫秒。默认全页，超过宽 4096、高 8192 或 1600 万像素时只截视口，并明确返回 `truncated`。一次性渲染限制为 40 秒；加载失败、HTTP 错误或取消不会返回成功截图。截图是某一时刻的画面，不等于完整的动画或交互测试。

成功结果包含截图路径、实际尺寸、标题和图像块；无需再发起一个模型调用才能把图片附入结果。现有执行循环在下一次模型请求中带上图像。已配置为纯文本的模型会收到“截图已保存，但无法视觉检查”的说明，不会伪称看到图片。未知模型能力仍遵守现有模型输入配置与供应商约束。

截图文件保存到集中历史目录的 `tool-results/<session-id>/browser/`；模型收到的图像同时复用已有不可变媒体存档、过程缩略图、放大与历史恢复机制。删除／移动会话仍沿用现有产物管理。图像预览不会调用模型。

## 桌面与 CLI

- 本机桌面 `render` 使用随应用分发的 Electron Chromium，无需另装 Playwright 或 Chromium。
- CLI/SSH 使用已有可选浏览器后端。源码安装可在仓库执行 `python -m pip install -e ".[browser]"`，然后由同一 Python 执行 `python -m playwright install chromium`。已有 Xuloom Python 安装也可添加其 `browser` extra。缺少依赖会明确报错，不假装生成截图。
- 原有 `navigate/snapshot/click/type/wait/screenshot/close` 交互浏览器仍使用 Playwright；本次新增的免安装入口仅为一次性 `render`。`screenshot` 操作现在也会返回图像块。

## SSH 执行端

远端需要使用含此实现的 Xuloom（`0.1.15` 起），并在 SSH 配置选择的 Python 中安装浏览器 extra 和 Chromium。Linux 还需按 [Playwright 系统依赖说明](https://playwright.dev/python/docs/browsers#install-system-dependencies)准备宿主依赖。框架不会自动 SSH 安装系统软件或把本机浏览器依赖复制到服务器。

App 和 `xuloom remote exec`／交互式 SSH CLI 使用同一 `browser_tool`：截图在执行主机生成，模型请求使用该主机已配置的连接和输入能力，图片回看复用已有 SSH 媒体与历史读取。文件路径、`localhost`、开发服务器和 Cookie 都不会改投本机；执行端缺少 Chromium、权限被拒绝或连接中断时保留明确错误，不回退到本机浏览器。原生分布式角色也沿用执行端的工具与授权边界。

CLI/SSH 的 `render` 每次建立临时、无持久化登录态的浏览器上下文，与交互式 `navigate/type` 的会话分离；结束、失败或取消后关闭该上下文。启动由原有浏览器管理器协调，共用已拥有的驱动，不增加代理服务、监听端口或第二套 Agent 生命周期。可选浏览器 SDK 与浏览器二进制不进入默认 Windows 发行包。

工作区 HTML 通过一次性拦截 origin 读取，复用现有 `html_preview` 的类型、30 MB 单资源限制及分块版本校验。目录穿越、隐藏文件、越界链接和本地文件协议不能作为页面资源读取。截图最多 20 MB。浏览器上下文隔离是登录态与资源读取边界，不代替操作系统级沙箱。

源码、本机实际 Chromium、加密 SSH、正式 CLI 和 App 的验证状态见 [远端截图验证](history/remote-capture-validation-2026-11-02.md)。该记录区分源码与安装版、协议服务与真实模型，以及原生跨平台验收。

## 权限与隔离

`render` 沿用 `browser_tool` 的高风险审批与执行规则，运行页面脚本不被标为“纯只读”。系统执行隔离模式继续拒绝整个浏览器工具，不绕过隔离调用宿主 Chromium。子代理继承的是相同的宿主截图通道，不增加工具权限。

每次桌面截图使用独立的临时内存浏览器会话，无用户登录态、应用 preload、Node.js 或主界面桥接。窗口保持隐藏，拒绝弹窗、下载、设备权限以及非 HTTP(S)／本次预览资源的协议请求。取消、超时、引擎退出会清理拥有的窗口；不会关闭用户自己的窗口。网页仍可在已授权的宿主浏览器环境访问网络；这不等于系统级离线沙箱。

## 本地验证（2026-10-31）

执行 `npm run test:browser-capture` 可运行隔离配置、隐藏窗口和本地协议模型替身的端到端验证：实际 HTML/CSS/SVG 像素、HTTP 页面、图像进入模型请求、缩略图与放大、取消清理、重启恢复、无用户 Cookie/Node 桥接、越界文件/危险协议/HTTP 错误拒绝。

Python 回归：`python -m unittest tests.test_browser_capture tests.test_tool_media`。没有调用真实远端模型，也不以本地协议替身证明所有第三方模型都能正确理解图像。当前成品验证平台为 Windows x64，未据此宣称 macOS/Linux 成品已经验证。

本次最终结果：Python 3.12 全量 373 项，372 通过、1 跳过；桌面逻辑 52 项通过；TypeScript/Vite 构建通过。源码与原 EXE 的截图端到端测试均通过，原程序包内 Python 的截图／媒体回归 9 项通过。成品构建 `20261031-115548`，版本仍为 `0.1.0`，保留原 EXE 与启动路径。

长页验证严格检查 800×1800 尺寸与 1750 像素处的底部色块，确认没有把视口拉长冒充全文。Windows 显示缩放统一到 CSS 像素；不同 Chromium/显示配置的暗色色块允许单通道 12/255 偏差。最终成品使用原生隐藏窗口 `capturePage`，不依赖调试协议截图。
