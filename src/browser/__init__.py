"""Surface seam: perceiving and acting on a UI. Only this package imports Playwright."""

from .browser import Browser, BrowserError, Element, FindResult, describe

__all__ = ["Browser", "BrowserError", "Element", "FindResult", "describe"]
