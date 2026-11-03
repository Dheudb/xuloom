import { File, FileCode, FileImage, FileText, Folder, Globe, Link } from "lucide-react";
export default function ReferenceIcon({ path, url }: { path?: string; url?: string }) {
  const host = url ? new URL(url).hostname.toLowerCase() : "";
  if (host === "github.com" || host.endsWith(".github.com")) return <svg width="14" height="14" viewBox="0 0 16 16" className="reference-icon" aria-hidden="true" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38v-1.49c-2.23.48-2.7-.95-2.7-.95-.36-.92-.89-1.17-.89-1.17-.73-.5.06-.49.06-.49.8.06 1.22.82 1.22.82.71 1.22 1.87.87 2.33.67.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.65 7.65 0 014 0c1.53-1.03 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.28.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.74.54 1.49v2.21c0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z" /></svg>;
  const Icon = url ? url.startsWith("xuloom:") ? Link : Globe
    : /[\\/]$/.test(path || "") ? Folder : /\.(png|jpe?g|gif|webp|svg)$/i.test(path || "") ? FileImage
    : /\.(py|tsx?|jsx?|c|cpp|rs|go|sh|html|css|json)$/i.test(path || "") ? FileCode
    : /\.(md|txt|pdf|docx?)$/i.test(path || "") ? FileText : File;
  return <Icon size={14} className="reference-icon" aria-hidden="true" />;
}
