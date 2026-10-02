"""FastAPI server package (build Step 14, Master sections 25 and 28).

Usage::

    from uap.server import create_app

    app = create_app()            # ./data/runs for events + artifacts
    app = create_app(run_inline=True)   # synchronous, for tests
"""

from .app import EventStream, create_app

__all__ = ["create_app", "EventStream"]
