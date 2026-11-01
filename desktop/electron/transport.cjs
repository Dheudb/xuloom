const { spawn } = require("node:child_process");
const readline = require("node:readline");
const path = require("node:path");

class EngineTransport {
  constructor({ python, engineRoot, project, onEvent, isolated = false, remote }) {
    this.pending = new Map();
    this.sequence = 0;
    this.closed = false;
    this.stderr = "";
    this.child = spawn(
      python,
      [...(isolated ? ["-I"] : []), "-u", "-m", remote ? "xuloom.remote.proxy" : "xuloom.desktop.bridge",
        ...(remote ? ["--connection", remote.id, "--binding", Buffer.from(JSON.stringify(remote.binding)).toString("base64"),
          ...(remote.machine ? ["--machine", Buffer.from(JSON.stringify(remote.machine)).toString("base64")] : [])] : [])],
      {
        cwd: project,
        windowsHide: true,
        stdio: ["pipe", "pipe", "pipe"],
        env: {
          ...process.env,
          PYTHONPATH: [engineRoot, process.env.PYTHONPATH]
            .filter(Boolean)
            .join(path.delimiter),
          PYTHONIOENCODING: "utf-8",
          PYTHONUNBUFFERED: "1",
        },
      },
    );
    this.lines = readline.createInterface({ input: this.child.stdout });
    this.exited = new Promise((resolve) => {
      this.child.once("exit", resolve);
      this.child.once("error", resolve);
    });
    this.lines.on("line", (line) => {
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        return;
      }
      if (message.event) {
        onEvent(message.event);
        return;
      }
      const pending = this.pending.get(message.id);
      if (!pending) return;
      clearTimeout(pending.timer);
      this.pending.delete(message.id);
      message.error
        ? pending.reject(new Error(message.error))
        : pending.resolve(message.result);
    });
    this.child.stderr.on("data", (chunk) => {
      this.stderr = (this.stderr + chunk.toString("utf8")).slice(-6000);
    });
    const fail = (message) => {
      if (this.closed) return;
      this.closed = true;
      for (const pending of this.pending.values()) {
        clearTimeout(pending.timer);
        pending.reject(new Error(message));
      }
      this.pending.clear();
      this.lines.close();
      if (!this.closing) onEvent({ type: "engine_exit", message });
    };
    this.child.on("error", (error) =>
      fail("Python 启动失败：" + error.message),
    );
    this.child.on("exit", (code) =>
      fail("Python 引擎已退出 (" + code + ")。" + this.stderr),
    );
    this.child.stdin.on("error", () => {});
  }
  request(method, params = {}) {
    if (this.closed) return Promise.reject(new Error("Engine is closed"));
    return new Promise((resolve, reject) => {
      const id = ++this.sequence;
      const timer = setTimeout(
        () => {
          this.pending.delete(id);
          reject(new Error("请求超时：" + method));
        },
        method === "mcp.reload" ? 180000 : 60000,
      );
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(JSON.stringify({ id, method, params }) + "\n");
    });
  }
  close() {
    return this.closePromise ||= this.dispose();
  }
  async dispose() {
    if (this.closed) return this.exited;
    this.closing = true;
    try {
      await this.request("shutdown");
    } catch (error) {
      this.closeError = error;
      this.closing = false;
      this.closePromise = undefined;
      throw error;
    }
    this.closeError = undefined;
    this.child.stdin.end();
    const timeout = setTimeout(() => {
      if (!this.closed) this.child.kill();
    }, 3000);
    await this.exited;
    clearTimeout(timeout);
  }
}
module.exports = { EngineTransport };
