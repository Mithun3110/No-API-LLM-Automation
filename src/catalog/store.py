"""Recipe files on disk: recipes/<recipe_id>@<version>.json. No LLM code here, on purpose:
`replay --recipe` loads recipes through this module and must never load an LLM client."""

import getpass
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from src.models import Recipe
from src.models.recipe import Approval

ROOT = Path(__file__).parent.parent.parent
RECIPES_DIR = ROOT / "recipes"
FILE_NAME = re.compile(r"^(?P<id>[a-z0-9_.]+)@(?P<version>\d+\.\d+\.\d+)\.json$")


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))  # "1.10.0" sorts after "1.9.0"


def write_recipe(recipe: Recipe, path: Path) -> None:
    """One format for every write, so diffs between versions stay readable for reviewers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(recipe.model_dump(mode="json", exclude_none=True), indent=2) + "\n")


@dataclass
class StoredRecipe:
    recipe: Recipe
    path: Path


@dataclass
class Catalog:
    """All recipe versions, grouped by id. The ACTIVE version of an id is its latest approved
    one; only if none is approved, its latest draft (which callers must treat as unreviewed)."""
    versions: dict[str, list[StoredRecipe]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)  # files that could not be loaded

    @classmethod
    def load(cls, recipes_dir: Path = RECIPES_DIR) -> "Catalog":
        catalog = cls()
        for path in sorted(recipes_dir.glob("*.json")):
            if not FILE_NAME.match(path.name):
                catalog.problems.append(f"{path.name}: name is not <recipe_id>@<x.y.z>.json")
                continue
            try:
                recipe = Recipe.model_validate_json(path.read_text())
            except ValidationError as e:
                # A broken file must not break the catalog, but it must not be silently ignored.
                catalog.problems.append(f"{path.name}: invalid recipe ({e.error_count()} error(s))")
                continue
            catalog.versions.setdefault(recipe.recipe_id, []).append(StoredRecipe(recipe, path))
        for stored in catalog.versions.values():
            stored.sort(key=lambda s: version_key(s.recipe.version))
        return catalog

    def ids(self) -> list[str]:
        return sorted(self.versions)

    def active(self, recipe_id: str) -> StoredRecipe | None:
        stored = self.versions.get(recipe_id, [])
        approved = [s for s in stored if s.recipe.status == "approved"]
        return (approved or stored or [None])[-1]

    def all_active(self) -> list[StoredRecipe]:
        return [self.active(i) for i in self.ids()]

    def for_app(self, app_origin: str) -> "Catalog":
        """Only the recipes recorded for this app (scheme://host:port of their entry URL)."""
        def same_app(s: StoredRecipe) -> bool:
            parts = urlsplit(s.recipe.app.entry_url)
            return f"{parts.scheme}://{parts.netloc}" == app_origin
        kept = {rid: [s for s in versions if same_app(s)] for rid, versions in self.versions.items()}
        return Catalog({rid: v for rid, v in kept.items() if v}, list(self.problems))

    def latest(self, recipe_id: str) -> StoredRecipe | None:
        stored = self.versions.get(recipe_id, [])
        return stored[-1] if stored else None


def approve(stored: StoredRecipe, basis: str) -> Recipe:
    """Mark one recipe version approved, recording who, when and why. Rewrites only that file."""
    approved = stored.recipe.model_copy(update={
        "status": "approved",
        "needs_review": False,  # approving IS the review
        "approval": Approval(approved_by=getpass.getuser(), approved_at=datetime.now(timezone.utc), basis=basis),
    })
    approved = Recipe.model_validate(approved.model_dump())  # re-validate before writing
    write_recipe(approved, stored.path)
    return approved
