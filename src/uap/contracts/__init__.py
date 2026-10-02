"""Core domain contracts (Master section 27) - implemented in build Step 1."""

from . import models
from .models import *  # noqa: F401,F403  (re-export the public contract surface)

__all__ = list(models.__all__)
