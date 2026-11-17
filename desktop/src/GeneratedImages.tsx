import MarkdownImage, { MarkdownImageContext } from "./MarkdownImage";
import { imageReference } from "./image-reference";
import type { FileChange } from "./types";
import { t } from "./i18n";

export default function GeneratedImages({ files, sessionId }: { files?: FileChange[]; sessionId: string }) {
  const paths = [...new Set((files || []).filter(file => file.operation === "generate" && imageReference(file.path).kind === "local").map(file => file.path))];
  if (!paths.length) return null;
  return <div className="generated-images" role="group" aria-label={t("生成的图片")}>
    <MarkdownImageContext.Provider value={{ sessionId }}>
      {paths.map(path => <MarkdownImage key={path} src={path} alt={path.replaceAll("\\", "/").split("/").pop()} />)}
    </MarkdownImageContext.Provider>
  </div>;
}
