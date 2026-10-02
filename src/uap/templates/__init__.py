"""Template catalog: real starting points instead of a blank canvas.

A *template* is a named starting setup a user picks before anything runs. The
catalog is data, not behaviour: it maps each template to a workflow that
actually exists and to the input shape the user must provide. Nothing here
executes anything -- the server's ``/api/templates`` routes read this module,
and ``POST /api/workspaces/from-template`` turns a template into a workspace
plus a real task.

Honesty rule (this is the whole point): a template is only listed when the
workflow it maps to can run today. The platform currently executes two
workflows -- ``research`` and ``bbp`` -- so every template maps to one of them.
A category with no real templates is omitted rather than faked.

Public surface::

    from uap.templates import catalog, template_detail, get_template, TEMPLATE_NAMES

    catalog()                     # the grouped catalog (ComfyUI shape)
    get_template("research_literature_review")   # one template dict, or None
    template_detail("...")        # template + workflow mapping + input shape
    TEMPLATE_NAMES                # every template name, in catalog order
"""

from .catalog import (
    CATALOG,
    PUBLIC_TEMPLATE_FIELDS,
    TEMPLATE_NAMES,
    catalog,
    get_template,
    template_detail,
)

__all__ = [
    "CATALOG",
    "PUBLIC_TEMPLATE_FIELDS",
    "TEMPLATE_NAMES",
    "catalog",
    "get_template",
    "template_detail",
]
