from __future__ import annotations

from rich.text import Text

SHOVEL_LOGO = r"""
███████╗██╗  ██╗ ██████╗ ██╗   ██╗███████╗██╗
██╔════╝██║  ██║██╔═══██╗██║   ██║██╔════╝██║
███████╗███████║██║   ██║██║   ██║█████╗  ██║
╚════██║██╔══██║██║   ██║╚██╗ ██╔╝██╔══╝  ██║
███████║██║  ██║╚██████╔╝ ╚████╔╝ ███████╗███████╗
╚══════╝╚═╝  ╚═╝ ╚═════╝   ╚═══╝  ╚══════╝╚══════╝
"""


def get_logo() -> Text:
    logo = Text()
    logo.append(SHOVEL_LOGO, style="bold dark_orange")
    return logo


def get_tagline() -> Text:
    text = Text()

    text.append(
        "\nDIG • BUILD • DISCOVER\n\n",
        style="bold #FFD700",
    )

    text.append(
        "🚀 Personal AI Agent Platform",
        style="bold bright_green",
    )

    return text
