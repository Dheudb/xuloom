const CHUNK = 262144;

function previewStream(transport, method, params, file, start, end, alive) {
  let cursor = start, stopped = false;
  return new ReadableStream({
    pull: async controller => {
      if (stopped) return;
      if (!alive()) { stopped = true; controller.error(new Error("文件预览已关闭")); return; }
      if (cursor > end) { stopped = true; controller.close(); return; }
      try {
        const length = Math.min(CHUNK, end - cursor + 1);
        const value = await transport.request(method, { ...params, version: file.version, offset: cursor, length });
        if (stopped) return;
        if (!alive()) throw new Error("文件预览已关闭");
        if (typeof value.data !== "string" || value.data.length > Math.ceil(length / 3) * 4) throw new Error("文件读取回执无效");
        const bytes = Buffer.from(value.data, "base64");
        if (bytes.toString("base64") !== value.data || value.offset !== cursor || value.nextOffset !== cursor + bytes.length ||
            value.size !== file.size || value.version !== file.version || !bytes.length || bytes.length > length)
          throw new Error("文件读取回执无效");
        cursor += bytes.length; controller.enqueue(bytes);
        if (cursor > end) { stopped = true; controller.close(); }
      } catch (error) { stopped = true; controller.error(error); }
    },
    cancel() { stopped = true; },
  });
}
module.exports = { previewStream };
