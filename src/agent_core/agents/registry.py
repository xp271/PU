"""AgentRegistry — multi-source definition store with priority-chain merging.

Four built-in source layers (ordered low → high priority):

* ``builtin`` — bundled agent definitions
* ``plugin``  — entry-point / plugin-registered agents
* ``project`` — per-project overrides (``.agent/agents/``)
* ``user``    — user-level overrides (``~/.agent/agents/``)

When the same *agent_type* is registered in multiple layers the
definitions are merged: scalar fields use "highest explicit-setter wins",
``disallowed_tools`` and ``auto_approve_tools`` are unioned across layers,
and ``mcp_servers`` is shallow-merged.
"""

from __future__ import annotations

from typing import Callable, Literal

from .definition import AgentDefinition

# Ordered low → high priority.
_SOURCE_ORDER: tuple[str, ...] = ("builtin", "plugin", "project", "user")

# Fields that are unioned (instead of overwritten) during merging.
_UNION_FIELDS = frozenset({"disallowed_tools", "auto_approve_tools"})

# Fields that are shallow-merged (dict update).
_MERGE_DICT_FIELDS = frozenset({"mcp_servers"})


class AgentRegistry:
    """Global agent-definition registry with priority-chain merging.

    Usage::

        registry = AgentRegistry()

        # Register in a specific layer
        registry.register(my_def, source="project")

        # Query the merged result (raises KeyError if unknown)
        merged = registry.get("Explore")

        # Check existence
        if registry.contains("my-agent"):
            ...
    """

    def __init__(self) -> None:
        # _layers[source][agent_type] = AgentDefinition
        self._layers: dict[str, dict[str, AgentDefinition]] = {
            src: {} for src in _SOURCE_ORDER
        }

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def register(
        self,
        definition: AgentDefinition,
        source: Literal["builtin", "plugin", "project", "user"] = "project",
    ) -> AgentDefinition:
        """Register *definition* in *source* layer.

        Same-source same-agent_type replaces the previous entry.
        Returns *definition* for use as a decorator/expression target.
        """
        if source not in self._layers:
            raise ValueError(
                f"Unknown source '{source}'. "
                f"Must be one of {list(_SOURCE_ORDER)}."
            )
        self._layers[source][definition.agent_type] = definition
        return definition

    def unregister(
        self,
        agent_type: str,
        source: str | None = None,
    ) -> None:
        """Remove *agent_type* from *source* layer.

        When *source* is ``None`` the type is removed from **all** layers.
        """
        if source is not None:
            self._layers.get(source, {}).pop(agent_type, None)
        else:
            for layer in self._layers.values():
                layer.pop(agent_type, None)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get(self, agent_type: str) -> AgentDefinition:
        """Return the merged definition for *agent_type*.

        Raises ``KeyError`` if the type is not registered in any layer.
        """
        layers = self._collect_layers(agent_type)
        if not layers:
            raise KeyError(
                f"Agent type '{agent_type}' is not registered. "
                f"Known types: {sorted(self._all_types())}"
            )
        return self._merge(layers)

    def list_all(self) -> dict[str, AgentDefinition]:
        """Return merged definitions for every known agent_type."""
        result: dict[str, AgentDefinition] = {}
        for at in sorted(self._all_types()):
            result[at] = self.get(at)
        return result

    def list_by_source(self, source: str) -> dict[str, AgentDefinition]:
        """Return raw (unmerged) definitions in *source* layer."""
        return dict(self._layers.get(source, {}))

    def contains(self, agent_type: str) -> bool:
        """Return ``True`` if *agent_type* is registered in any layer."""
        return agent_type in self._all_types()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _all_types(self) -> set[str]:
        types: set[str] = set()
        for layer in self._layers.values():
            types.update(layer)
        return types

    def _collect_layers(self, agent_type: str) -> list[AgentDefinition]:
        """Collect non-None definitions from each layer, low → high priority."""
        result: list[AgentDefinition] = []
        for src in _SOURCE_ORDER:
            d = self._layers[src].get(agent_type)
            if d is not None:
                result.append(d)
        return result

    # ------------------------------------------------------------------
    # Merge algorithm
    # ------------------------------------------------------------------

    @staticmethod
    def _merge(layers: list[AgentDefinition]) -> AgentDefinition:
        """Merge multiple definitions using priority-chain rules.

        *layers* must be ordered low → high priority.

        **Rules** (per plan §4.2):

        1.  ``agent_type`` is preserved from layer 0.
        2.  Scalar fields — highest explicit-setter wins.
        3.  ``disallowed_tools`` / ``auto_approve_tools`` — unioned.
        4.  ``mcp_servers`` — shallow-merge (higher-priority wins on same key).
        5.  ``tools`` / ``skills`` / ``hooks`` — highest explicit-setter wins.
        """
        if not layers:
            raise ValueError("Cannot merge empty layer list")

        # Start from layer 0 full dump (includes defaults as base).
        merged = layers[0].model_dump()

        # Low → high: apply only fields that were *explicitly set*.
        for layer in layers[1:]:
            overrides = layer.model_dump(exclude_unset=True)

            for field_name, value in overrides.items():
                if field_name == "agent_type":
                    continue  # unchanged (all layers agree)

                if field_name in _UNION_FIELDS:
                    # Union: start from current merged value + append new
                    existing = set(merged.get(field_name, []))
                    merged[field_name] = sorted(existing | set(value))

                elif field_name in _MERGE_DICT_FIELDS:
                    # Shallow merge: higher-priority keys win
                    merged[field_name] = {**merged.get(field_name, {}), **value}

                else:
                    # Scalar / list — higher-priority overwrites
                    merged[field_name] = value

        return AgentDefinition.model_validate(merged)


# ------------------------------------------------------------------
# Module-level default (singleton)
# ------------------------------------------------------------------

_default_registry = AgentRegistry()


# ------------------------------------------------------------------
# Convenience functions (delegating to the default registry)
# ------------------------------------------------------------------

def register_agent(
    definition: AgentDefinition | None = None,
    *,
    source: str = "project",
) -> Callable | AgentDefinition:
    """Register an AgentDefinition in the global default registry.

    Dual-use — works as a direct call or a decorator::

        # Direct call (preferred for built-in agents):
        register_agent(my_def, source="builtin")

        # Decorator (advanced — wraps an expression):
        @register_agent(source="project")
        MY_AGENT = AgentDefinition(agent_type="my-agent", ...)

    When called without *definition* (bare ``@register_agent(...)``) a
    decorator is returned that captures the result of the assignment and
    calls ``register()`` on it.
    """
    if definition is not None:
        # Direct-call path
        return _default_registry.register(definition, source=source)  # type: ignore[return-value]

    # Decorator path — definition is ``None``, so this is ``@register_agent(...)``.
    def _decorator(d: AgentDefinition) -> AgentDefinition:
        _default_registry.register(d, source=source)  # type: ignore[arg-type]
        return d

    return _decorator


def get_agent(agent_type: str) -> AgentDefinition:
    """Look up a merged ``AgentDefinition`` from the default registry."""
    return _default_registry.get(agent_type)


def list_agents() -> dict[str, AgentDefinition]:
    """List all merged agent definitions in the default registry."""
    return _default_registry.list_all()
