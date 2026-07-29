"""Result-only application facade for portable profile operations."""

from __future__ import annotations

from dataclasses import dataclass

from omr_grader.application.dto import ProfileImportRequest, ProfileImportResult
from omr_grader.domain.errors import Result
from omr_grader.domain.profile import Profile
from omr_grader.infrastructure.profile_store import ProfileCatalogItem, ProfileStore


@dataclass(frozen=True, slots=True)
class ProfileApplicationService:
    """Thin application boundary; validation and filesystem details stay in ProfileStore."""

    store: ProfileStore

    def import_profile(self, request: ProfileImportRequest) -> Result[ProfileImportResult]:
        return self.store.import_profile(request)

    def discover_profiles(self) -> Result[tuple[str, ...]]:
        return self.store.discover()

    def profile_catalog(self) -> Result[tuple[ProfileCatalogItem, ...]]:
        """Shared Screen 1/4 catalog; invalid entries retain diagnostics for settings."""
        return self.store.catalog()

    def load_profile(self, filename: str) -> Result[Profile]:
        return self.store.load(filename)

    def default_profile(self, filename: str) -> Result[Profile | None]:
        return self.store.default_profile(filename)


ProfileUseCaseService = ProfileApplicationService

__all__ = ["ProfileApplicationService", "ProfileUseCaseService"]
