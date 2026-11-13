import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("FRAMEKEEP_DATA_DIR", ".framekeep")).resolve()
    )
    allow_private_hosts: set[str] = field(default_factory=set)
    trust_fake_dns: bool = field(
        default_factory=lambda: os.getenv("FRAMEKEEP_TRUST_FAKE_DNS") == "1"
    )
    admin_token: str = field(default_factory=lambda: os.getenv("FRAMEKEEP_ADMIN_TOKEN", ""))
    allowed_hosts: list[str] = field(
        default_factory=lambda: [
            h.strip()
            for h in os.getenv(
                "FRAMEKEEP_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver"
            ).split(",")
            if h.strip()
        ]
    )
    max_download_bytes: int = field(
        default_factory=lambda: int(os.getenv("FRAMEKEEP_MAX_DOWNLOAD_BYTES", str(2 * 1024**3)))
    )
    min_free_bytes: int = field(
        default_factory=lambda: int(os.getenv("FRAMEKEEP_MIN_FREE_BYTES", str(512 * 1024**2)))
    )
    static_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parents[1] / "dist")

    def prepare(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
