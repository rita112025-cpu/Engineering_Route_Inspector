"""Application settings. The server only ever listens on the local machine."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
DEFAULT_PORT = 8765


class ConfigError(ValueError):
    pass


def default_data_dir() -> Path:
    return Path(os.environ.get("ERI_DATA_DIR") or REPO_ROOT / "data")


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    max_drawing_mb: int = 200
    max_document_mb: int = 50
    max_json_kb: int = 5 * 1024
    max_workers: int = 1
    static_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "static")
    demo_dir: Path = field(default_factory=lambda: REPO_ROOT / "demo")

    def __post_init__(self):
        if self.host not in LOOPBACK_HOSTS:
            raise ConfigError(f"只允許本機連線（{', '.join(LOOPBACK_HOSTS)}），不接受 {self.host!r}。"
                              "此工具處理工程資料，不對網路開放。")
        if not (1 <= int(self.port) <= 65535):
            raise ConfigError("連接埠必須介於 1 到 65535")
        object.__setattr__(self, "data_dir", Path(self.data_dir))

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Host header values accepted (blocks DNS rebinding: a hostile page cannot reach us under its own name)."""
        p = self.port
        return frozenset({f"127.0.0.1:{p}", f"localhost:{p}", f"[::1]:{p}"})

    @property
    def allowed_origins(self) -> frozenset[str]:
        return frozenset(f"http://{h}" for h in self.allowed_hosts)

    @property
    def url(self) -> str:
        host = "[::1]" if self.host == "::1" else self.host
        return f"http://{host}:{self.port}/"
