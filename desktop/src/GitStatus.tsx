import { useEffect, useRef, useState } from "react";
import { GitBranch } from "lucide-react";
import { t, tServer } from "./i18n";

type Snapshot = { available: boolean; reason?: string; code?: string; root?: string; branch?: string;
  commit?: string; unborn?: boolean; detached?: boolean; dirty?: boolean; staged?: number;
  unstaged?: number; untracked?: number; conflicted?: number; ahead?: number; behind?: number };

export default function GitStatus({ sessionId, project, busy }: { sessionId: string; project: string; busy: boolean }) {
  const [state, setState] = useState<Snapshot>();
  const refresh = useRef<() => void>(() => {});
  useEffect(() => {
    let cancelled = false, pending = false;
    const read = async () => {
      if (pending) return;
      pending = true;
      try {
        const value = await window.xuloom.invoke("git.status", { session: sessionId });
        if (!cancelled) setState(value);
      } catch {
        if (!cancelled) setState({ available: false, code: "git_error", reason: t("Git 状态暂不可用，点击重试") });
      } finally { pending = false; }
    };
    refresh.current = () => void read();
    void read();
    const tick = () => { if (document.visibilityState === "visible") void read(); };
    const timer = window.setInterval(tick, 15000);
    window.addEventListener("focus", tick);
    return () => { cancelled = true; clearInterval(timer); window.removeEventListener("focus", tick); };
  }, [sessionId, project, busy]);
  if (!state || state.code === "not_repository" || state.code === "workspace_missing") return null;
  const label = state.available ? state.detached ? `HEAD · ${state.commit?.slice(0, 7)}` : state.branch || "Git" : t("Git 不可用");
  const details = state.available ? [state.root, state.detached ? "Detached HEAD" : state.branch,
    state.unborn ? t("尚无提交") : state.commit?.slice(0, 12),
    t("暂存 {staged} · 未暂存 {unstaged} · 未跟踪 {untracked}", { staged: state.staged, unstaged: state.unstaged, untracked: state.untracked }),
    state.conflicted ? t("未解决冲突 {conflicted}", { conflicted: state.conflicted }) : "",
    state.ahead != null ? t("领先 {ahead} · 落后 {behind}（本地已知状态）", { ahead: state.ahead, behind: state.behind }) : "", t("点击刷新")] : [tServer(state.reason)];
  return <button className="git-status" aria-label={t("Git 状态")} title={details.filter(Boolean).join("\n")} onClick={() => refresh.current()}>
    <GitBranch size={13} /><span>{label}</span>{state.dirty && <i aria-label={t("有未提交变更")}>●</i>}
  </button>;
}
