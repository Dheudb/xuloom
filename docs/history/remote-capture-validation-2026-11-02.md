# SSH 截图与一次性渲染验证（2026-11-02）

本轮范围为 `0.1.15` 的可选 CLI/SSH 网页截图。使用实际系统 OpenSSH、加密回环 SSH 服务、Python 3.12、Playwright `1.64.1` 和其 Chromium headless shell；模型请求只访问受控本地 HTTP/SSE，不使用个人模型账号或真实供应商额度。物理两机、Linux/macOS 原生与真实视觉理解仍未据此验证。

## 实现

- SSH 桥不再把远端 `render` 绑到本机 Electron 回调。在执行主机使用既有可选 Playwright 后端，客户端始终拒绝把远端截图事件改交本机浏览器。
- CLI/SSH 的一次性截图建立独立非持久化上下文，不复用交互浏览器的 Cookie。浏览器启动加互斥，避免并发启动覆盖已有驱动句柄；新上下文由同一管理器拥有并在结束、取消与关闭时清理。
- 工作区 HTML 使用拦截 origin 复用现有 HTML 预览的文件类型、范围、30 MB 单资源和分块版本检查，替换直接加载 file:// 的路径。没有新建 HTTP 服务、浏览器控制服务或第二套调度器。
- 图像沿用原有中央产物、原生工具回执、模型输入能力与 App 媒体读取。纯文本模型取得保存位置和限制说明，拒绝／失败／取消不生成成功图像。
- 可选 SDK 与浏览器只安装在测试/执行主机，默认发行包不增加它们。系统隔离模式继续拒绝浏览器工具，独立浏览器上下文不等于系统级隔离。

## 检查范围

`tests/test_browser_capture.py` 保留原有 8 项依赖、回调、取消和模型能力检查。`tests/test_browser_render.py` 用真实 Chromium 覆盖 HTML/CSS/SVG 像素与 console、HTTPS 拦截 origin、无 Node、隐藏文件与 file:// 拒绝、Cookie 隔离、HTTP 错误、并发、取消、真实整页底部像素及超大页面截断。

`tests/test_remote_capture.py` 使用原有 SSH 夹具与实际远端桥，覆盖正式 CLI、远端 HTML 图片进入模型输入、图片与历史重开、纯文本能力、缺失资源、用户拒绝和已加载页面的停止。`desktop/tests/remote-capture.e2e.cjs` 以隐藏窗口操作生产 App 入口：文件和 URL 实际渲染、图片缩略图／放大、历史重开、页面请求无模型 Key／Cookie，并直接禁止本机渲染器或本机读取远端路径作为断言。

## 初轮失败与复验

首轮 SSH 断言误把 `turn_finished` 通知当作包含 `ok` 的最终执行回执；已改为读取实际 `final.result`。权限专项也曾以旧错误文字断言失败，实际已有 `permission_behavior: deny` 且没有图片，现以回执字段验证。均保留原始失败日志，不能按错误文字推断工具已经越权。

本机交互式 Windows 环境的输入法辅助程序会暂时继承已关闭浏览器的工作目录，造成测试清理时的共享冲突。原始进程核对显示本次 Node/Chromium 已退出；测试只在已确认的临时根目录等待共享句柄释放，不结束输入法或其他用户进程。初期清理失败和等待日志保留；这不是远端执行完成状态的判据。

源码 App 的一次测试启动还曾使用错误的相对 Python 路径，得到 ENOENT；改用已有解释器的绝对路径后完整通过，没有以跳过截图代替复验。

## 最终验证与安装

最终运行时代码冻结后，Python 3.12 全量 **684 项：683 通过、1 项 Windows 文件符号链接权限跳过**，耗时 1090.185 秒。此轮同时启用了真实 Chromium、系统 OpenSSH 和已安装 Codex／Claude Code／Pi 的本地受控接口检查，没有调用真实模型供应商。新增的 7 项 Chromium 与 6 项远端截图用例包含在此全量中。Node 逻辑 72 项通过，TypeScript／Vite 构建通过；构建仍提示单个压缩 JS 块超过 500 kB，未把这条体积提示改写为构建失败或零风险。

暂存资源及更新后原安装分别以原程序的 Python 执行 34 项浏览器契约、HTML、图像存档、存储和实例连接检查，全部通过；断言所有 Xuloom 模块实际来自所测资源，同时确认默认环境不包含 Playwright 或测试用 Paramiko。真实 Chromium 检查使用开发执行端的可选依赖，不能算作默认包内已包含浏览器。

源码 App 与更新后原 EXE 均完成本机截图和加密 SSH 截图的隐藏窗口流程。远端测试客户端使用实际应用，执行端使用安装了可选浏览器的开发 Python；执行端代码与原安装逐文件一致。源码 SSH HTML／正式 CLI 额外回归也通过。另把实际更新后原程序 Python 作为 SSH 执行端，在未装可选浏览器时确认明确缺依赖、无 PNG、无本机截图回退。

原 EXE 资源已在应用正常退出后更新到 **0.1.15，构建 `20261102-055530`**，保留事务回滚副本。启动文件 SHA256 仍为 `2529b494f4123f152bce346051f7bd855ef95006424a561953cf17c49a3d1ba8`。166 个 Python／Markdown、62 个前端资源和 18 个 Electron 文件与公开源码／构建逐字节一致。只包含原有 9 个公开内置 Skill；没有访问或打包私人 Taiyi Skill、配置、聊天、浏览器 SDK／二进制。

本地日志分别为 `desktop/build/v0115-full-python.log`、`remote-capture-node-r1.log`、`v0115-{stage,installed}-python.log`、`v0115-source-remote-ui-r2.log`、`v0115-source-remote-html.log`、`v0115-installed-remote-ui.log`、`v0115-installed-local-capture.log` 和 `v0115-original-remote-missing.log`；逐文件核对为 `v0115-installed-resource-equivalence.json`。日志不进入程序或公开包。

源码提交、公开发布及下载资产是独立步骤；成品以 [v0.1.15 Release](https://github.com/Dheudb/xuloom/releases/tag/v0.1.15) 的 `RELEASE.json`、资产大小和 SHA256 为准，不能仅凭此文档判断网络上传成功。

DSH、外部 CLI 经 SSH 全组合、完整外部中间历史 UI、跨平台安装和真实模型长任务继续在[完整路线图](../product-roadmap.md)中，未被截图专项替代。
