import { useEffect, useState } from 'react';
import { t, tServer, tx } from "./i18n";

type Info = { version: string; buildId: string; sourceCommit: string; sourceTreeSha256: string; dirty: boolean | null;
  validation: { present: boolean; scope: string; checks: Record<string, unknown>; realProvidersTested: boolean } };

export default function BuildDetails() {
  const [info, setInfo] = useState<Info>();
  const [error, setError] = useState('');
  useEffect(() => { let alive = true;
    window.xuloom.invoke('app.build-info').then(value => { if (alive) setInfo(value); }).catch(() => { if (alive) setError(t("无法读取随包构建记录")); });
    return () => { alive = false; };
  }, []);
  return <details className="build-details"><summary>{t("程序版本与验证")}</summary>
    {error ? <p role="status">{tServer(error)}</p> : !info ? <p>{t("正在读取本地记录…")}</p> : <>
      <p>{tx("版本：{value} · 构建：{value2}", { value: info.version || t("开发版"), value2: info.buildId || t("开发环境") })}</p>
      <p>{t("源码提交：")}<code>{info.sourceCommit || t("未附带提交记录")}</code>{info.dirty === true && t("（含未提交修改）")}</p>
      {info.sourceTreeSha256 && <p style={{overflowWrap:'anywhere'}}>{t("源码指纹：")}<code>{info.sourceTreeSha256}</code></p>}
      <p>{info.validation.present ? info.validation.scope : t("未附带与本构建对应的验证记录")}</p>
      {info.validation.present && <pre>{JSON.stringify(info.validation.checks, null, 2)}</pre>}
      <p className="fine-print">{info.validation.realProvidersTested ? t("随包记录含供应商测试；请查看具体范围。") : t("未进行本构建的真实模型验收；本地测试不代表真实任务质量或跨平台保证。")}</p>
    </>}
  </details>;
}
