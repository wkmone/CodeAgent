from codecli.config.config import CodeCliConfig
from codecli.tools.builtins import get_builtin_tools
from codecli.tools.registry import ToolRegistry


async def build_tool_registry(
        *,
        config: CodeCliConfig,
        cwd: str
) -> tuple[ToolRegistry, None]:
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())

    return registry, None