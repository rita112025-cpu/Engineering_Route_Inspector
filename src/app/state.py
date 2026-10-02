from __future__ import annotations

import time
from dataclasses import dataclass, field

from jobs.manager import JobManager
from persistence.db import Database
from persistence.repo import Repo
from persistence.storage import Storage
from .config import AppConfig


@dataclass
class AppState:
    config: AppConfig
    storage: Storage
    db: Database
    manager: JobManager
    token: str
    recovery: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)

    def repo(self) -> Repo:
        """Repository bound to the calling thread's own database connection."""
        return self.db.repo()
