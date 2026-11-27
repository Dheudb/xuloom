"""Interactive media management and explicit generation over the bundled plugin."""
from getpass import getpass

from xuloom.media import config, jobs

KINDS = {"image": "图片 / Image", "video": "视频 / Video", "speech": "语音 / Speech", "sound": "音效 / Sound", "music": "音乐 / Music"}


def choose(tui, title, rows):
    from rich.markup import escape
    return tui.choose_menu(title=title, body_lines=[], prompt_label="选择 / Select", default_value="",
        options=[{"value": key, "label": escape(label)} for key, label in rows] + [{"value": "", "label": "取消 / Cancel"}])


def model(tui):
    kind = choose(tui, "生成用途 / Media type", list(KINDS.items()))
    if not kind:
        return None
    rows = [row for row in config.public_models() if row["kind"] == kind or kind == "image" and row["kind"] == "image-edit"]
    if not rows:
        raise ValueError("No model configured for this type; use /media add / 此用途尚未配置模型")
    identity = choose(tui, "选择生成模型 / Select media model", [(str(index), row["model"] + " · " + row["base_url"])
        for index, row in enumerate(rows)])
    return rows[int(identity)] if identity else None


async def command(engine, text, tui, console):
    from xuloom.terminal_actions import require_idle, paths
    from xuloom.media.plugin import run_submit, run_control
    require_idle(engine)
    if "media_tool" not in engine.available_tools and "media_tool" not in engine.latent_tools:
        raise ValueError("Media plugin is disabled; use xuloom plugins enable media / 媒体插件未启用")
    argument = text.strip().partition(" ")[2].strip()
    action, _, identity = argument.partition(" ")
    action = action.lower()
    if not action:
        action = choose(tui, "媒体 / Media", [(key, label) for key, label in (
            ("generate", "选择模型并生成 / Generate"), ("list", "模型列表 / Models"), ("add", "添加模型 / Add model"),
            ("edit", "编辑模型 / Edit model"), ("remove", "移除模型 / Remove model"), ("jobs", "本会话任务 / Session jobs"),
            ("status", "查询任务 / Query job"), ("cancel", "请求取消任务 / Request cancellation"))])
    if not action:
        return
    if action == "list":
        console.print_json(data={"models": config.public_models()})
        return
    if action in {"jobs", "status", "cancel"}:
        records = jobs.listing(engine.tool_context)
        if action == "jobs":
            console.print_json(data={"jobs": records})
            return
        identity = identity.strip() or choose(tui, "选择已有任务 / Saved job", [(row["id"], row["id"] + " · " + row["phase"]) for row in records])
        if identity:
            receipt, record = await run_control(engine, action, identity)
            console.print_json(data={"job": record, "receipt": receipt})
        return
    if action not in {"add", "edit", "remove", "generate"}:
        raise ValueError("Use /media generate|list|add|edit|remove|jobs|status|cancel")
    row = None if action == "add" else model(tui)
    if action != "add" and row is None:
        return
    if action == "remove":
        if choose(tui, "仅移除配置，不取消已有任务 / Remove configuration only", [("yes", "确认移除 / Confirm removal")]):
            config.remove_model(row["connectionId"], row["id"])
        return
    if action in {"add", "edit"}:
        kind = row["kind"] if row else choose(tui, "生成用途 / Media type", list(KINDS.items()))
        if not kind:
            return
        base = console.input("API URL (Enter to keep): ").strip() or (row["base_url"] if row else "")
        name = console.input("Model (Enter to keep): ").strip() or (row["model"] if row else "")
        key = getpass("API Key (leave blank to keep saved key): ")
        value = {"kind": kind, "base_url": base, "model": name, "apiKey": key}
        if row:
            value.update(connectionId=row["connectionId"], id=row["id"])
        if choose(tui, "保存配置，不发起生成 / Save without generation", [("yes", "保存 / Save")]):
            config.save_model(value)
            console.print("已保存 / Saved", markup=False)
        return
    prompt = console.input("生成内容 / Prompt (empty cancels): ").strip()
    if not prompt:
        return
    fields = {"image": ("size", "aspect_ratio"), "image-edit": ("size", "aspect_ratio"), "video": ("duration_seconds", "aspect_ratio"),
              "speech": ("voice", "speed"), "sound": ("duration_seconds",), "music": ("duration_seconds",)}
    inputs = {}
    for field in fields[row["kind"]]:
        value = console.input(field + " (Enter for default): ").strip()
        if value:
            inputs[field] = float(value) if field in {"duration_seconds", "speed"} else value
    images = paths(console.input("参考图片路径 / Reference images (optional, quoted paths): ").strip()) if row["kind"] in {"image", "image-edit", "video"} else []
    output = console.input("输出目录 / Output directory (Enter: generated/): ").strip() or None
    values = {"connection": row["connectionId"], "model": row["id"], "prompt": prompt, "inputs": inputs, "image_paths": images, "output_dir": output}
    if not choose(tui, "提交会产生供应商用量 / Submission may incur provider charges", [("yes", "提交一次 / Submit once")]):
        return
    receipt, record = await run_submit(engine, values)
    console.print_json(data={"job": record, "receipt": receipt})
    console.print("查询使用 /media status；停止本地等待不等于供应商取消。 / Stopping local waiting does not cancel the provider.", markup=False)
