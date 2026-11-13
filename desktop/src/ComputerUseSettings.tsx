import { useEffect, useState } from "react";
import { t, tServer } from "./i18n";

type Settings = { supported: boolean; enabled: boolean };

export default function ComputerUseSettings() {
  const [settings, setSettings] = useState<Settings>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    window.xuloom.invoke<Settings>("computerUse.get", {}).then(result => {
      if (active) setSettings(result);
    }).catch(error => {
      if (active) setError(String(error));
    });
    return () => { active = false; };
  }, []);

  async function toggle(enabled: boolean) {
    setBusy(true);
    setError("");
    try {
      setSettings(await window.xuloom.invoke<Settings>("computerUse.set", { enabled }));
    } catch (error) {
      setError(String(error));
    } finally {
      setBusy(false);
    }
  }

  if (!settings?.supported && !error) return null;
  return <section className="language-setting">
    {settings?.supported && <>
      <label className="computer-use-toggle">
        <input type="checkbox" checked={settings.enabled} disabled={busy}
          onChange={event => void toggle(event.target.checked)} />
        {t("启用 Windows 桌面操控")}
      </label>
      <p className="fine-print">{t("对新开的本机会话生效。操作前会请求窗口授权，截图会发送给所选模型。操作时使用你的鼠标和键盘，可随时停止任务。")}</p>
    </>}
    {error && <p role="alert" className="error">{tServer(error)}</p>}
  </section>;
}
