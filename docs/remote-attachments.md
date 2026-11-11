# SSH 会话附件

本机和远端均升级到 0.1.13 后，SSH 聊天支持拖入文件、选择文件及粘贴图片。传输复用系统 OpenSSH、原有 JSONL 运行时与中央历史，不新增服务器或 SDK。握手的 `runtime.info.attachments == 1` 表示远端支持此协议；旧远端明确要求升级，不把本机路径发送给远端模型。

## 桌面

在 SSH 聊天的输入区添加文件，等待上传完成后发送。图片点击后在 App 内预览；普通文件显示“保存副本”，点击选择本机保存位置。不会在本机尝试打开远端路径，也不会自动运行下载文件。

附件副本属于远端会话的中央历史。删除本机原文件不影响已上传副本，未发送附件的 ID 随草稿保存在本机导航引用中；重开会话后向原远端核对。上传和预览不创建模型请求，用户发送消息后才进入现有模型输入和权限流程。

## CLI

```sh
# 上传并返回会话 ID、附件 UUID 和校验和，不运行模型。
xuloom remote attach lab ./paper.pdf ./diagram.png --session SESSION --json

# 查询已保存附件；超过一页时以 nextCursor 传入 --cursor。
xuloom remote attachments lab --session SESSION --json
xuloom remote download lab ATTACHMENT ./local-copy.pdf --session SESSION --json

# 传输本机文件和引用此前上传的 UUID，再提交一次任务。
xuloom remote exec lab "分析附件" --session SESSION --attach ./diagram.png --attachment ATTACHMENT --json
```

`attach` 可以省略 `--session` 创建一个空远端会话，首先输出 `session_started`，再输出每个文件的 `attachment_started`，最后返回回执。即使完成确认丢失，也可使用已输出的会话/附件 ID 重新查询；不自动重放上传或 prompt。`download` 和 `attachments` 必须指定会话；`--resume` 延续原任务，不能替换原来的附件。

交互 `xuloom remote chat lab --session SESSION` 支持：

```text
/attach C:\research\paper.pdf
/attachments
/attachments use ATTACHMENT
/attachments clear
/download ATTACHMENT "C:\research\paper copy.pdf"
```

`/attach` 将上传成功的附件选入下一条消息，消息提交被接受后清空选择。`/attachments use` 可复用已保存附件；`clear` 仅移除下一条消息的选择，不删除远端副本。文件名包含空格时，`/attach` 后的整段文本视为一个路径。普通 prompt 仍在原输入位置输入；不生成额外的“继续”消息。

## 程序接入

在[已有 SSHClient](remote.md#程序接口)连接并初始化会话后，复用与 App／CLI 相同的客户端；本机路径只交给本机传输函数，模型收到的是远端会话附件引用。

```python
from xuloom.remote.attachments import upload, download

items = await upload(client, [{"path": "./diagram.png"}])
await client.request("prompt", {"text": "分析这张图", "attachments": [item["id"] for item in items]})
# 下载不执行模型；目标文件只在完整校验后替换。
await download(client, items[0]["id"], "./saved-diagram.png")
```

## 可靠性和权限边界

- 每条消息最多 10 个附件；单图 20 MB、普通文件 64 MB，与本机既有限制一致。图片在发布前验证，并按已有图片输入规则处理；上传成功不意味着所选模型一定支持视觉输入。
- 文件按 256 KB 块传输，校验顺序、大小及 SHA256，再发布 ready 清单；未完成、损坏或来源文件中途变化时不能成为模型输入。下载先写同目录临时文件，完整校验后才替换用户选择的目标，失败保留原目标。
- 上传复用会话独占写锁。另一写者占用、任务正在执行、受控子代理历史等情况明确拒绝。只读查询、下载仍使用会话绑定的附件 ID；不能指定任意远端读取路径。
- 取消后停止后续传输，正在写盘的工作先收尾再释放锁。断线后的状态可能是完成、未完成或未知；恢复时查询原 ID，不因为没有收到确认就宣称失败或重放。批量中每个文件独立提交，前面的成功文件可保留，不伪装整批原子事务。
- App 的“取消附件传输”针对实际上传会话，即使用户切到其他聊天也不会停止错误的任务。CLI 可用 Ctrl+C 取消；上传失败后重新查询、移除未完成 ID，再显式重传。
- 这是附件一致性与会话授权约束，不是对恶意同权限宿主进程的操作系统沙箱。

本次协议、故障及加密 SSH App／CLI 的证据见[验证记录](history/remote-attachments-validation-2026-11-02.md)。物理两机、Linux/macOS 原生及真实模型视觉质量仍需单独验收，不能以回环测试代替。
