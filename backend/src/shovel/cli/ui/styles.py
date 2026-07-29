from __future__ import annotations

from rich.theme import Theme

SHOVEL_CLI_THEME = Theme(
    {
        "success": "bold green",
        "error": "bold red",
        "warning": "bold yellow",
        "info": "cyan",
        "debug": "dim",
        "muted": "gray50",
        "title": "bold blue",
        "hightlight": "bold magenta",
    }
)
