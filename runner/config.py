from dataclasses import dataclass, field
import json
from pathlib import Path


@dataclass
class Config:
    application_id: int
    guild_id: int
    data_dir: str
    token_file: str
    channel_name: str = "classpoint-screenshotter"
    channel_id: int | None = None
    allowed_user_ids: list[int] = field(default_factory=list)
    poll_seconds: float = 1.0
    settle_seconds: float = 0.6
    end_grace_seconds: float = 15
    disconnect_grace_seconds: float = 120
    max_session_hours: float = 12
    max_slides: int = 3000
    min_free_mb: int = 512
    status_update_seconds: float = 600

    @classmethod
    def load(cls, path: Path):
        config = cls(**json.loads(path.read_text()))
        if config.poll_seconds < 0.1 or config.settle_seconds < 0:
            raise ValueError("Invalid polling settings")
        if min(config.end_grace_seconds, config.disconnect_grace_seconds,
               config.max_session_hours, config.max_slides, config.min_free_mb,
               config.status_update_seconds) <= 0:
            raise ValueError("Runner limits must be positive")
        return config

    def token(self):
        path = Path(self.token_file).expanduser()
        if path.stat().st_mode & 0o077:
            raise ValueError("Bot token file must be private (chmod 600)")
        token = path.read_text().strip()
        if not token or any(c.isspace() for c in token):
            raise ValueError("Bot token file is empty or invalid")
        return token
