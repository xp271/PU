"""Built-in agent definitions.

Importing this sub-package triggers registration of the three built-in
agents (GeneralPurpose, Explore, Plan) into the default global registry.
"""

from . import explore, general, plan  # noqa: F401 — side-effect: register_agent()

__all__ = ["explore", "general", "plan"]
