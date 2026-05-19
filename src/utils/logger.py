from __future__ import annotations

import logging
import sys

from rich.logging import RichHandler


def get_logger(name: str = "harness") -> logging.Logger:
    """返回带 RichHandler 的共享 logger，并避免重复安装 handler。"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = RichHandler(
            show_path=False,
            show_time=True,
            markup=True,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
