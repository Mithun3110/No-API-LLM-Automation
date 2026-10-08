"""Manual check of the browser layer: python -m src.browser.demo [member_id]

Opens a visible browser on the bank (must be running on :5050), signs in, prints the
accessibility tree the agent will see, then finds and clicks elements by role and name.
"""

import os
import sys

from dotenv import load_dotenv

from src.browser import Browser
from src.models.recipe import LabelStrategy, NearTextStrategy, RoleStrategy

BANK = "http://localhost:5050"


def main() -> None:
    load_dotenv()
    member_id = sys.argv[1] if len(sys.argv) > 1 else "12345"
    with Browser(headless=False, slow_mo_ms=400) as b:  # slow_mo so you can watch
        b.goto(BANK + "/login")
        # Credentials come from .env and are never printed.
        b.type(b.find([LabelStrategy(by="label", text="User ID")]).element, os.environ["BANK_USERNAME"])
        b.type(b.find([LabelStrategy(by="label", text="Password")]).element, os.environ["BANK_PASSWORD"])
        b.click(b.find([RoleStrategy(by="role", role="button", name="Sign On")]).element)

        print("\n=== Accessibility tree (compact, what the agent sees) ===")
        print(b.compact_snapshot())

        box = b.find([RoleStrategy(by="role", role="textbox", name="Member ID")])
        button = b.find([RoleStrategy(by="role", role="button", name="Search")])
        print("\n=== Finding elements ===")
        print(" ", box.attempts, "\n ", button.attempts)
        b.type(box.element, member_id)
        b.click(button.element)

        balance = b.find([NearTextStrategy(by="near_text", anchor="Savings Balance:")])
        print(" ", balance.attempts)
        if balance.found:
            print(f"\nSavings balance for member {member_id}: {b.read_text(balance.element)}")
        else:
            print(f"\nNo balance found. Page says: {b.page.locator('h2').first.inner_text()}")
        input("\nPress Enter to close the browser...")


if __name__ == "__main__":
    main()
