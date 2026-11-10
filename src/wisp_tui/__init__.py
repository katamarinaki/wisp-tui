"""wisp-tui: a terminal chat client for the local Wisp runtime."""

import os
import re

__version__ = "0.2.2"


def program_name() -> str:
    """The name the launcher was invoked as (e.g. a `wi` symlink)."""
    name = os.environ.get("WISP_TUI_PROG", "")
    return name if re.fullmatch(r"[A-Za-z0-9._-]{1,32}", name) else "wisp-tui"
