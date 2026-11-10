from abc import ABC, abstractmethod
from typing import Type, Any, Dict
import os
from pydantic import BaseModel

class BaseAgenticTool(ABC):
    """
    The fundamental interface that all Xuloom tools must inherit. 
    Pydantic inputs are projected into each supported provider's native tool schema.
    """
    
    # Must be defined strictly by subclasses
    name: str = ""
    description: str = ""
    input_schema: Type[BaseModel] = BaseModel
    is_read_only: bool = False
    risk_level: str = "high"
    # Trusted declarations, never guessed from model or MCP argument names.
    # True means an omitted directory defaults to the engine workspace.
    workspace_paths: Dict[str, bool] = {}

    @property
    def context(self) -> Dict[str, Any]:
        """Engine-provided runtime context; each tool instance owns its own binding."""
        value = self.__dict__.get("_context")
        if value is None:
            value = self.__dict__["_context"] = {}
        return value

    @context.setter
    def context(self, value: Dict[str, Any]):
        self.__dict__["_context"] = value

    def prepare_input(self, values):
        values = dict(values)
        sandbox = self.context.get("sandbox_manager")
        cwd = self.context.get("session_cwd") or os.getcwd()
        for name, default_directory in self.workspace_paths.items():
            value = values.get(name)
            if not value and not default_directory:
                continue
            if sandbox and sandbox.active_sandbox:
                values[name] = (sandbox.resolve_working_directory(value) if default_directory
                    else sandbox.resolve_path(value))
            else:
                values[name] = os.path.abspath(os.path.join(cwd, value or "."))
        return values
    
    @classmethod
    def get_tool_definition(cls) -> Dict[str, Any]:
        """
        Dynamically generates the provider-neutral JSON schema specification
        by interrogating the Pydantic input_schema Model.
        """
        if not cls.name or not cls.description:
            raise ValueError(f"Tool {cls.__name__} must define 'name' and 'description'")
            
        schema = cls.input_schema.model_json_schema()
            
        return {
            "name": cls.name,
            "description": cls.description,
            # Keep $defs and constraints: nested models reference them via $ref.
            "input_schema": schema
        }
        
    @abstractmethod
    async def execute(self, **kwargs) -> Any:
        """
        Core async invocation method.
        Must be implemented by inherited tools.
        """
        pass

    def is_read_only_call(self, **kwargs) -> bool:
        return self.is_read_only

    def is_concurrency_safe_call(self, **kwargs) -> bool:
        return self.is_read_only_call(**kwargs)

    def get_risk_level(self, **kwargs) -> str:
        return self.risk_level

    def build_permission_summary(self, **kwargs) -> str:
        preview = []
        for key, value in list(kwargs.items())[:4]:
            rendered = str(value)
            if len(rendered) > 160:
                rendered = rendered[:157] + "..."
            preview.append(f"{key}: {rendered}")

        if not preview:
            return self.description

        return self.description + "\n" + "\n".join(preview)

    def prompt(self) -> str:
        return ""

    def validate_input(self, **kwargs):
        return None

    def input_error_hint(self) -> str:
        """Optional tool-specific repair guidance, used only before execution."""
        return ""

    def check_permissions(self, permission_context: Dict[str, Any]):
        return None
        
    async def __call__(self, **kwargs):
        """
        Executes the tool with automated Pydantic schema coercion and validation.
        """
        sandbox = self.context.get("sandbox_manager")
        res_str = ""
        kwargs = self.prepare_input(self.input_schema(**kwargs).model_dump())
        if sandbox and sandbox.active_sandbox and self.workspace_paths:
            res_str = f"[Workspace routing: {sandbox.active_sandbox['branch']}; this is not an OS security sandbox.]"
        
        normalized_kwargs = kwargs
        validation_error = self.validate_input(**normalized_kwargs)
        if isinstance(validation_error, str) and validation_error.strip():
            raise ValueError(validation_error)

        raw_res = await self.execute(**normalized_kwargs)

        if isinstance(raw_res, dict) and {"ok", "content", "metadata", "is_error"}.issubset(raw_res.keys()) and res_str:
            enriched = dict(raw_res)
            enriched["content"] = res_str + "\n" + str(enriched.get("content", ""))
            metadata = dict(enriched.get("metadata", {}) or {})
            metadata["sandbox_branch"] = sandbox.active_sandbox["branch"] if sandbox and sandbox.active_sandbox else None
            metadata["sandbox_path"] = sandbox.active_sandbox["path"] if sandbox and sandbox.active_sandbox else None
            enriched["metadata"] = metadata
            return enriched
        
        if isinstance(raw_res, str) and res_str:
            if self.name == "command_tool":
                # Control receipts must stay JSON. Their cwd already records workspace routing.
                return raw_res
            return res_str + "\n" + raw_res
        return raw_res
