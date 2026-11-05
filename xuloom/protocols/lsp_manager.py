import os
from typing import Dict, Optional

from xuloom.protocols.lsp_client import LSPBridge

class LSPManager:
    """
    Manages multiple Language Server sub-processes simultaneously.
    Routes requests to appropriate servers based on file extension.
    Passive Diagnostics receiver. Provides continuous telemetry back to the LLM agent.
    """
    def __init__(self):
        self.servers: Dict[str, LSPBridge] = {}
        # absolute_path -> list of diagnostic errors/warnings
        self.diagnostics_registry: Dict[str, list] = {}
        self._new_diagnostics_flag = False
        
        # Native mapping logic for "Full Stack" ecosystem scaling
        self.language_map = {
            ".py": ("pyright-langserver", ["--stdio"], "python"),
            ".ts": ("typescript-language-server", ["--stdio"], "typescript"),
            ".tsx": ("typescript-language-server", ["--stdio"], "typescript"),
            ".js": ("typescript-language-server", ["--stdio"], "javascript"),
            ".jsx": ("typescript-language-server", ["--stdio"], "javascript")
        }
        
    def _diagnostic_callback(self, uri: str, diagnostics: list):
        """Asynchronous snare tracking 'textDocument/publishDiagnostics'"""
        if uri and str(uri).startswith("file://"):
            # Strip standard URL mapping to Windows-compatible Local Path
            raw_path = uri.replace("file:///", "").replace("file://", "")
            if os.name == 'nt' and ":" in raw_path[:3]:
                # Windows disk drive correction
                raw_path = raw_path
            
            # Unescaped URI
            import urllib.parse
            file_path = os.path.abspath(urllib.parse.unquote(raw_path))
            
            self.diagnostics_registry[file_path] = diagnostics
            self._new_diagnostics_flag = True
        
    async def get_or_create_server(self, file_path: str) -> Optional[tuple[LSPBridge, str]]:
        """Returns (LSPBridge_Instance, language_id) or None if unsupported file extension."""
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in self.language_map:
            return None
            
        cmd, args, lang_id = self.language_map[ext]
        
        if cmd not in self.servers:
            bridge = LSPBridge(cmd, args)
            bridge.set_diagnostic_callback(self._diagnostic_callback)
            success = await bridge.start()
            if success:
                self.servers[cmd] = bridge
            else:
                return None
                
        return self.servers[cmd], lang_id
        
    def consume_diagnostics(self) -> str:
        """Pops aggregated compiler errors formatting a prompt-ready warning snippet for LLM loop injection."""
        if not self._new_diagnostics_flag:
            return ""
            
        lines = []
        for path, diags in self.diagnostics_registry.items():
            if not diags: continue
            
            # Severity 1 = Error, Severity 2 = Warning
            important_diags = [d for d in diags if d.get('severity', 1) in [1, 2]]
            if not important_diags: continue
                
            lines.append(f"File: {path}")
            # Max 5 diagnostics per file to prevent blowing out Claude's API token limit
            for d in important_diags[:5]: 
                sev_type = "ERROR" if d.get('severity', 1) == 1 else "WARNING"
                msg = d.get('message', '').replace('\\n', ' ')
                # 0-indexed mapped to 1-indexed for human-like reading
                line = d.get('range', {}).get('start', {}).get('line', 0) + 1
                lines.append(f"  [{sev_type}] Line {line}: {msg}")
                
        self._new_diagnostics_flag = False
        
        if not lines:
            return ""
            
        return "LSP Diagnostics Monitor Alert. The last changes caused new underlying issues:\\n" + "\\n".join(lines)
