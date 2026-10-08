"""Manual check of the session gate: python -m src.handoff.demo [member_id] [delay_ms]

Signs in from .env, searches a member, reads the balance, then tries two things the gate
must stop: a URL outside the allowlist, and a risky Confirm click (rejected: no approver yet).
Everything is logged, masked, to runs/<run_id>/log.jsonl.

delay_ms (default 800) slows every browser action so you can watch; 0 = full speed.
Runs on its own: it announces each part, pauses briefly, then carries on.
"""

import sys

from src.handoff import Action, open_session
from src.models.recipe import LabelStrategy, NearTextStrategy, RoleStrategy


def announce(session, message: str, seconds: float = 2) -> None:
    """Say what happens next, then give the viewer time to read it before it happens."""
    print(f"\n>>> {message}")
    session.browser.wait(seconds)  # not time.sleep: keeps the browser responsive


def main() -> None:
    member_id = sys.argv[1] if len(sys.argv) > 1 else "12347"
    delay_ms = int(sys.argv[2]) if len(sys.argv) > 2 else 800
    with open_session("replay", slow_mo_ms=delay_ms) as s:
        announce(s, f"Signed in. Next: search member {member_id} and read the balance.")
        s.perform(Action("navigate", url="/search", reason="open search"))
        s.perform(Action("type", (RoleStrategy(by="role", role="textbox", name="Member ID"),), value=member_id,
                         reason="enter member id"))
        s.perform(Action("click", (RoleStrategy(by="role", role="button", name="Search"),), reason="search"))
        out = s.perform(Action("extract", (NearTextStrategy(by="near_text", anchor="Savings Balance:"),),
                               reason="read balance"))
        print(f"\n  Savings balance (shown to you, masked in the log): {out.text}")

        announce(s, "Next: try a URL outside the allowlist (/_admin/inject). It should be BLOCKED.")
        s.perform(Action("navigate", url="/_admin/inject?error=server_error", reason="try a forbidden URL"))

        announce(s, "Next: fill in an Open Sub-Account form, then try Confirm. It should be REJECTED.")
        s.perform(Action("navigate", url=f"/member/{member_id}/open-account", reason="open form"))
        s.perform(Action("select", (RoleStrategy(by="role", role="combobox", name="Account Type"),),
                         value="Money Market", reason="choose type"))
        s.perform(Action("type", (LabelStrategy(by="label", text="Initial Deposit"),), value="100.00",
                         reason="enter deposit"))
        s.perform(Action("click", (RoleStrategy(by="role", role="button", name="Continue"),), reason="review"))
        s.perform(Action("click", (RoleStrategy(by="role", role="button", name="Confirm"),), reason="confirm"))

        print(f"\n  Still on the review page: nothing was opened. Log: {s.logger.folder.log_path}")
        announce(s, "Done. Closing the browser in 5 seconds.", seconds=5)


if __name__ == "__main__":
    main()
