"""Canonical Clara → Clive orchestration entrypoint.

The implementation remains in ``clara_clse_orchestrator`` so existing imports,
persisted jobs, and the deprecated ``clara-clse`` launcher keep working.
"""

from hermes_cli import clara_clse_orchestrator as _impl
from hermes_cli.clara_clse_orchestrator import *  # noqa: F403
from hermes_cli.clara_clse_orchestrator import main


def __getattr__(name: str):
    """Forward legacy-private helpers while the implementation is shared."""
    return getattr(_impl, name)


if __name__ == "__main__":
    raise SystemExit(main())
