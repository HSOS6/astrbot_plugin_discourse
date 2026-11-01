"""Discourse forum plugin for AstrBot."""

from .adapter import DiscourseAdapter
from .main import DiscourseForumPlugin

__all__ = ["DiscourseForumPlugin", "DiscourseAdapter"]
