"""E8-011: the time screen as in Harvest – a day with week tabs, Track time, Start and Stop on a row.

Reached from the sidebar: Time > Track. Registered as the optional blueprint ``e8_time`` and as the module
``e8_time`` (Admin > Modules). With the module off, Track goes back to the dashboard and /time answers 404.
"""

from flask import Blueprint

from app.utils.module_registry import ModuleCategory, ModuleDefinition, ModuleRegistry

MODULE_ID = "e8_time"

e8_time_bp = Blueprint("e8_time", __name__, url_prefix="/time", template_folder="templates")

from e8.time_screen import routes  # noqa: E402,F401  (routes attach to the blueprint)

# Registered last: if anything above fails to import, the module stays unknown, is_module_enabled() is
# False and the sidebar keeps Track on the dashboard instead of linking to a missing route.
ModuleRegistry.register(
    ModuleDefinition(
        id=MODULE_ID,
        name="Time screen (Harvest-style)",
        description="Day view of your hours with Track time, Start and Stop on each row",
        category=ModuleCategory.TIME_TRACKING,
        blueprint_name="e8_time",
        default_enabled=True,
        icon="fa-clock",
        order=11,
    )
)
