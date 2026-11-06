# 发布包体积与精简边界

Xuloom 保留 Python 引擎，Windows 桌面自带 Electron、Python、模型 SDK、MCP、PDF 和图片依赖。运行时体积与核心代码大小、模型上下文长度是不同指标。

v0.1.36 的精简只发生在构建、退出后的资源更新和发行包生成中，不改变 Agent 的工具、提示词或模型请求。

## 保留与移出

- 保留原 `Xuloom.exe`、全部运行 DLL/PYD、Chromium 语言包、证书、字体、许可证及依赖元数据。
- 保留 PDF 读取/渲染、AVIF/WebP 等图像格式、MCP、系统凭据、所有已安装模型 SDK 接口。
- 只移出明确列出的依赖开发资源：MuPDF 编译头文件/链接库、PyWin32 开发帮助和头文件、指定依赖自带的测试/示例。不会按“任意目录叫 tests/docs”进行全局删除；Anthropic 的运行时 workspaces/tests 等 API 模块保留。
- 生成的 Python 字节码缓存不随包发布；本地缓存可由后续运行重新生成，不能把一次缓存清理当成永久的体积收益。
- 原安装的文件先核对暂存时记录的哈希，再移动到资源备份。发生修改或越界时拒绝处理；替换失败时连同这些文件一起回退。用户配置、Key、历史和私人 Skill 不在清理范围。

规则集中在 `desktop/scripts/package_content.py`，全新打包、原安装更新和发布 ZIP 复用同一份规则。

## 标准 ZIP 压缩

默认使用 Python 标准库的 Deflate level 9。发布机也可提供受信任的 7-Zip 控制台路径：

```text
python desktop/scripts/release.py --tag v0.1.36 --zip-program 构建机上的7za.exe路径
```

两条路径都输出标准 Deflate ZIP，不能切换为 ZIP-LZMA、Deflate64、加密格式或首次启动下载依赖。7-Zip 是可选的构建期工具，不放进应用，也不要求用户安装。原 EXE 的路径和二进制不变；这次缩小的是随包资源和压缩后的下载体积。

发布前以 Windows 自带的解压能力核对完整 ZIP，比较上一发行包中的原生程序、语言、证书和许可证字节，验证 PDF、图片、MCP、凭据、CLI、桌面及停止恢复。体积收益按最终完整 ZIP 与全新解压目录比较；缓存、回退备份、不同压缩格式不混入百分比。

本次不裁剪语言支持，不让 PDF/图片功能变成另行安装的可选依赖，也不为缩小文件替换 Electron 或重写 Python 引擎。保留这些条件时，包仍会包含完整桌面浏览器运行时，不能据此承诺 50 MB 的桌面分发。
