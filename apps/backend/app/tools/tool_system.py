"""Application-level tool system container."""

from dataclasses import dataclass

from app.config.settings import BackendSettings
from app.tools.tool_execute.tool_scheduler import ToolScheduler
from app.tools.tool_handler.read_file import build_read_file_definition
from app.tools.tool_registry import ToolRegistry


@dataclass(frozen=True)
class ToolSystem:
    """Hold the process-wide tool registry and execution scheduler.

    Parameters:
        registry: Registry containing every tool definition available to the
            application process.
        scheduler: Policy-aware scheduler used by runtime workflows to list
            and execute tools.

    Returns:
        ToolSystem instance.

    Raises:
        None.

    Side effects:
        None.
    """

    registry: ToolRegistry
    scheduler: ToolScheduler

    @classmethod
    def build_tool_system(self, settings: BackendSettings):
        """Build and register the process-wide tool system.

        Parameters:
            settings: Backend settings used by tool handlers to bind local paths
                and runtime limits.
            logger: Application logger shared by tool execution services.

        Returns:
            ToolSystem containing the initialized registry and scheduler.

        Raises:
            OSError: If a tool constructor fails while resolving required paths.

        Side effects:
            Creates an in-memory tool registry and registers built-in tools.
        """

        registry = ToolRegistry()
        registry.register(build_read_file_definition(settings.project_root))
        scheduler = ToolScheduler(
            registry=registry,
            allowed_permissions=("safe_read",),
        )
        return ToolSystem(registry=registry, scheduler=scheduler)
