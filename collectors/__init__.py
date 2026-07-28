"""Every collector that knows how to gather one group of UBC data.

Importing this package registers all collectors in `base.REGISTRY`; `update.py`
drives them from there. To add a source, drop a module in this directory with a
`@register`ed `Collector` subclass and add it to the imports below.
"""

from .base import (
    CAMPUS_CHOICES,
    DATA_DIR,
    REGISTRY,
    Collector,
    Http,
    Output,
    register,
    selected_campus,
    set_campus,
    utcnow,
    wants,
)

# Imported for their registration side effects; order sets the default run order.
from . import geospatial  # noqa: F401
from . import courses  # noqa: F401
from . import academic_calendar  # noqa: F401
from . import learning_spaces  # noqa: F401
from . import events  # noqa: F401
from . import admissions  # noqa: F401
from . import people  # noqa: F401
from . import services  # noqa: F401
from . import reports  # noqa: F401

__all__ = [
    "CAMPUS_CHOICES",
    "DATA_DIR",
    "REGISTRY",
    "Collector",
    "Http",
    "Output",
    "register",
    "selected_campus",
    "set_campus",
    "utcnow",
    "wants",
]
