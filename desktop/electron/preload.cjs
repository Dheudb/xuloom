const { contextBridge, ipcRenderer, webUtils } = require("electron");
contextBridge.exposeInMainWorld("xuloom", {
  flushDrafts: (drafts) => ipcRenderer.sendSync("workbench:drafts", drafts),
  importFiles: async (session, files, existing, ids) => {
    if (!Array.isArray(files) || files.length > 10) throw new Error("每条消息最多添加 10 个附件");
    const inputs = await Promise.all(files.map(async (file) => {
      const path = webUtils.getPathForFile(file);
      if (path) return { path };
      if (!/^image\/(png|jpeg|webp|gif|bmp)$/.test(file.type) || file.size > 20 * 1024 * 1024) throw new Error("请先将文件保存到本地；粘贴图片最大支持 20 MB");
      return { data: "data:" + file.type + ";base64," + Buffer.from(await file.arrayBuffer()).toString("base64") };
    }));
    return ipcRenderer.invoke("workbench:invoke", "attachments.import", { session, files: inputs, existing, ids });
  },
  invoke: (method, params) =>
    ipcRenderer.invoke("workbench:invoke", method, params || {}),
  onEvent: (callback) => {
    const handler = (_event, payload) => callback(payload);
    ipcRenderer.on("workbench:event", handler);
    return () => ipcRenderer.removeListener("workbench:event", handler);
  },
});
