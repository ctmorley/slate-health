"""Deterministic tool executor for agent actions.

Validates tool call parameters against registered tool definitions,
executes the tool function, and returns structured results. This ensures
agents can only invoke pre-approved actions with valid parameters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from app.core.engine.state import ToolCall, ToolResult

logger = logging.getLogger(__name__)


class ToolExecutionError(Exception):
    """Raised when tool execution fails."""
    pass


class ToolNotFoundError(ToolExecutionError):
    """Raised when a requested tool is not registered."""
    pass


class ToolValidationError(ToolExecutionError):
    """Raised when tool call parameters fail validation."""
    pass


@dataclass
class ToolDefinition:
    """Definition of a tool that agents can invoke.

    Attributes:
        name: Unique tool identifier.
        description: Human-readable description for LLM context.
        parameters: JSON Schema-style parameter definitions.
        required_params: List of required parameter names.
        handler: Async function that executes the tool.
    """

    name: str
    description: str
    parameters: dict[str, dict[str, Any]]
    required_params: list[str]
    handler: Callable[..., Awaitable[Any]]


class ToolExecutor:
    """Validates and executes tool calls from agent LLM reasoning.

    Maintains a registry of available tools and their schemas, validates
    incoming tool calls against those schemas, and executes the handler
    functions with validated parameters.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self,
        name: str,
        *,
        description: str = "",
        parameters: dict[str, dict[str, Any]] | None = None,
        required_params: list[str] | None = None,
        handler: Callable[..., Awaitable[Any]],
    ) -> None:
        """Register a tool for agent use.

        Args:
            name: Unique tool name.
            description: Description shown to the LLM.
            parameters: Dict mapping param name to schema (type, description).
            required_params: Params that must be present in every call.
            handler: Async function to execute when tool is called.
        """
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters or {},
            required_params=required_params or [],
            handler=handler,
        )
        logger.debug("Registered tool: %s", name)

    def register_tool(self, tool_def: ToolDefinition) -> None:
        """Register a pre-built ToolDefinition."""
        self._tools[tool_def.name] = tool_def

    @property
    def available_tools(self) -> list[str]:
        """List names of all registered tools."""
        return list(self._tools.keys())

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Get tool schemas formatted for LLM context.

        Returns a list of tool descriptions suitable for including
        in system prompts or tool-use APIs.
        """
        schemas = []
        for tool in self._tools.values():
            schemas.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": {
                        "type": "object",
                        "properties": tool.parameters,
                        "required": tool.required_params,
                    },
                }
            )
        return schemas

    def validate_tool_call(self, tool_call: ToolCall) -> list[str]:
        """Validate a tool call against its definition.

        Returns a list of validation error strings (empty if valid).
        """
        errors: list[str] = []

        tool_name = tool_call.get("tool_name", "")
        if not tool_name:
            errors.append("tool_name is required")
            return errors

        if tool_name not in self._tools:
            errors.append(f"Unknown tool: '{tool_name}'")
            return errors

        tool_def = self._tools[tool_name]
        params = tool_call.get("parameters", {})

        # Check required parameters
        for req in tool_def.required_params:
            if req not in params:
                errors.append(
                    f"Missing required parameter '{req}' for tool '{tool_name}'"
                )

        # Check parameter types
        for param_name, param_value in params.items():
            if param_name in tool_def.parameters:
                expected_type = tool_def.parameters[param_name].get("type", "")
                if expected_type and not self._check_type(param_value, expected_type):
                    errors.append(
                        f"Parameter '{param_name}' for tool '{tool_name}' "
                        f"expected type '{expected_type}', "
                        f"got '{type(param_value).__name__}'"
                    )

        return errors

    @staticmethod
    def _check_type(value: Any, expected_type: str) -> bool:
        """Check if a value matches the expected JSON Schema type."""
        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        expected = type_map.get(expected_type)
        if expected is None:
            return True  # Unknown type — skip validation
        return isinstance(value, expected)

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        """Validate and execute a single tool call.

        Args:
            tool_call: The tool call to execute.

        Returns:
            ToolResult with success status and result or error.
        """
        tool_name = tool_call.get("tool_name", "unknown")

        # Validate first
        errors = self.validate_tool_call(tool_call)
        if errors:
            error_msg = "; ".join(errors)
            logger.warning("Tool validation failed for '%s': %s", tool_name, error_msg)
            return ToolResult(
                tool_name=tool_name,
                success=False,
                result=None,
                error=error_msg,
            )

        tool_def = self._tools[tool_name]
        params = tool_call.get("parameters", {})

        try:
            result = await tool_def.handler(**params)
            logger.info("Tool '%s' executed successfully", tool_name)
            return ToolResult(
                tool_name=tool_name,
                success=True,
                result=result,
                error=None,
            )
        except Exception as exc:
            logger.error("Tool '%s' execution failed: %s", tool_name, exc)
            return ToolResult(
                tool_name=tool_name,
                success=False,
                result=None,
                error=str(exc),
            )

    async def execute_many(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """Execute multiple tool calls sequentially.

        Returns results in the same order as the input tool calls.
        """
        results = []
        for tc in tool_calls:
            result = await self.execute(tc)
            results.append(result)
        return results
