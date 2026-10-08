"""The safety policy, loaded from config/policy.json. Enforced in code by src/safety/."""

from typing import Literal

from pydantic import Field

from .common import PolicyAction, StrictModel


class Policy(StrictModel):
    allowed_domains: list[str] = Field(min_length=1)  # host:port, e.g. localhost:5050
    allowed_paths: list[str] = Field(min_length=1)    # glob patterns, e.g. /member/*
    allowed_actions: list[PolicyAction] = Field(min_length=1)
    risky_button_names: list[str] = []                # clicks on these need human approval
    risky_requires: Literal["human_approval"] = "human_approval"
    mask_fields: list[str] = []                       # field names whose values are masked in logs
