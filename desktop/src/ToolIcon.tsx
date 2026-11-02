import { Image, Pencil, Terminal, Search, FileText, Globe, Bot, Puzzle, Wrench, Workflow } from "lucide-react";
import type { ToolKind } from "./tool-presentation";
export default function ToolIcon({ kind }: { kind: ToolKind }) {
  const Icon = { sequence: Workflow, image: Image, edit: Pencil, terminal: Terminal, search: Search, file: FileText, web: Globe, agent: Bot, skill: Puzzle, tool: Wrench }[kind];
  return <Icon size={15} strokeWidth={1.6} className="tool-kind-icon" aria-hidden="true" data-tool-kind={kind} />;
}
