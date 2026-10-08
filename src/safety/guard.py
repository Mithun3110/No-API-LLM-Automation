"""The policy check every action passes before the browser runs it.

The LLM (or a recipe) PROPOSES an action; this module DECIDES. It is plain code with no
LLM involved, so a prompt injection on a page cannot talk its way past it.
Recipes are checked too, every time: being safe when recorded does not make them safe now.
"""

import json
import posixpath
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from src.models import Policy

DEFAULT_POLICY_PATH = Path(__file__).parent.parent.parent / "config" / "policy.json"

Verdict = Literal["allow", "needs_approval", "block"]


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> Policy:
    return Policy.model_validate(json.loads(path.read_text()))


@dataclass(frozen=True)
class ProposedAction:
    action: str                      # navigate, click, type, select, extract, wait
    page_url: str                    # the page the action happens on
    target_url: str | None = None    # navigate only: where it goes
    target_role: str | None = None   # e.g. button, link, textbox
    target_name: str | None = None   # accessible name, e.g. "Confirm"
    step_risk: str = "safe"          # from the recipe step: safe | irreversible


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    rule: str     # which rule decided, for the log, e.g. "risky_button"
    reason: str   # one readable sentence

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


class SafetyGuard:
    def __init__(self, policy: Policy):
        self.policy = policy
        # Whole-word, case-insensitive: "Confirm" matches "Confirm Transfer" but not "Unconfirmed".
        self._risky = [re.compile(rf"\b{re.escape(n)}\b", re.IGNORECASE) for n in policy.risky_button_names]

    def check(self, proposed: ProposedAction, allow_risky: bool = True) -> Decision:
        """allow_risky=False is for bounded LLM recovery, which may never do a risky step."""
        if proposed.action not in self.policy.allowed_actions:
            return Decision("block", "action_not_allowed", f"action '{proposed.action}' is not in the allowlist")

        # Acting on a page outside the allowlist is blocked, not only navigating to one.
        # One exception: a fresh browser starts on about:blank, and the only thing allowed
        # from there is navigating to an allowed page (checked just below).
        starting = proposed.page_url == "about:blank" and proposed.action == "navigate"
        problem = None if starting else self.url_problem(proposed.page_url)
        if problem:
            return Decision("block", "page_not_allowed", f"current page is outside the allowlist: {problem}")

        if proposed.action == "navigate":
            if not proposed.target_url:
                return Decision("block", "no_target_url", "navigate needs a destination URL")
            problem = self.url_problem(proposed.target_url)
            if problem:
                return Decision("block", "url_not_allowed", f"destination is outside the allowlist: {problem}")

        if proposed.action == "click" and self.is_risky(proposed):
            what = f'"{proposed.target_name}"' if proposed.target_name else "this element"
            if not allow_risky:
                return Decision("block", "risky_in_recovery", f"click on {what} changes data; recovery may not do that")
            return Decision("needs_approval", "risky_button",
                            f"click on {what} may change data and needs human approval")

        return Decision("allow", "allowed", "within policy")

    def is_risky(self, proposed: ProposedAction) -> bool:
        if proposed.step_risk == "irreversible":
            return True
        # Only buttons submit changes in this app; links like "Update Member Info" just open a form.
        # Unknown role is treated like a button: when in doubt, be conservative.
        if proposed.target_role not in (None, "button"):
            return False
        name = proposed.target_name or ""
        return any(p.search(name) for p in self._risky)

    def url_problem(self, url: str) -> str | None:
        """None if the URL is allowed, otherwise a short reason."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return f"scheme '{parts.scheme}' is not allowed"
        host = parts.netloc.lower()
        if host not in (d.lower() for d in self.policy.allowed_domains):
            return f"domain '{host}' is not allowed"
        # Normalise first so "/member/../_admin/inject" cannot sneak past a "/member/*" rule.
        path = posixpath.normpath(parts.path or "/")
        if parts.path.endswith("/") and path != "/":
            path += "/"
        if not any(fnmatchcase(path, pattern) for pattern in self.policy.allowed_paths):
            return f"path '{path}' is not allowed"
        return None
