from __future__ import annotations

from os import PathLike
from pathlib import PurePath
from typing import Union


def is_relative_to(path: PurePath, *other: Union[str, PathLike[str]]):
    """
    Returns true if path is relative to base.
    """
    try:
        path.relative_to(*other)
        return True
    except ValueError:
        return False
