# Windows Computer Use

Computer Use 让主 Agent 查看和操作你授权的本机 Windows 窗口。默认关闭。桌面端在「设置 → 工作区」打开「启用 Windows 桌面操控」，然后新建会话。交互式 CLI 可先运行 `xuloom plugins enable computer-use`，再启动 `xuloom`。关闭开关或执行 `xuloom plugins disable computer-use` 会阻止后续工具调用。

打开要操作的应用，并选择一个支持图片输入的模型。例如可以说「用 Computer Use 在记事本窗口里输入一行测试文字，再截图检查」。Agent 列出窗口，第一次截图前会显示窗口标题并请求本次任务授权。请把目标窗口放到前台再执行鼠标或键盘操作。截图会进入会话记录，并发送给所选模型。

工具支持窗口截图、移动指针、左键单击、滚轮、Unicode 文本输入和常用单键。每张截图可用于一次输入。窗口移动、缩放、键盘焦点变化或点击位置被遮挡时，应重新观察。工具回执确认输入已发送，Agent 需要再看窗口确认结果。点击停止可取消后续操作，已经送达应用的输入会保留。

第一版适用于本机交互式 Windows 桌面。工具使用系统真实指针与键盘，需要解锁桌面。最小化、受保护、无法用 PrintWindow 捕获或高权限的窗口可能无法操作。SSH、子代理及工作区隔离模式不提供宿主桌面操控。普通网页交互仍可用 [浏览器工具](browser-capture.md)。

工具借鉴 [Pi Computer Use](https://github.com/injaneity/pi-computer-use/blob/67e9e2f77643f84605b738a3198048a88e92190e/README.md) 的观察后操作方式，采用 Windows API 和已随应用分发的 Pillow，不需要额外安装运行时。
