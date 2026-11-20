from dataclasses import dataclass, field
import json
import math
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
    max_concurrent_sessions: int = 3
    inactivity_notice_seconds: float = 1800
    max_capture_scale: float = 2.0

    @classmethod
    def load(cls, path: Path):
        config = cls(**json.loads(path.read_text()))
        for key in ("poll_seconds", "settle_seconds", "end_grace_seconds", "disconnect_grace_seconds",
                    "max_session_hours", "min_free_mb", "status_update_seconds", "inactivity_notice_seconds",
                    "max_capture_scale"):
            value = getattr(config, key)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{key} must be a finite number")
        if config.poll_seconds < 0.1 or config.settle_seconds < 0:
            raise ValueError("Invalid polling settings")
        if not 1 <= config.max_capture_scale <= 2:
            raise ValueError("max_capture_scale must be between 1 and 2")
        if min(config.end_grace_seconds, config.disconnect_grace_seconds,
               config.max_session_hours, config.max_slides, config.min_free_mb,
               config.status_update_seconds, config.inactivity_notice_seconds) <= 0:
            raise ValueError("Runner limits must be positive")
        if type(config.max_concurrent_sessions) is not int or not 1 <= config.max_concurrent_sessions <= 10:
            raise ValueError("max_concurrent_sessions must be an integer from 1 to 10")
        if type(config.max_slides) is not int or config.max_slides <= 0:
            raise ValueError("max_slides must be a positive integer")
        return config

    def token(self):
        path = Path(self.token_file).expanduser()
        if path.stat().st_mode & 0o077:
            raise ValueError("Bot token file must be private (chmod 600)")
        token = path.read_text().strip()
        if not token or any(c.isspace() for c in token):
            raise ValueError("Bot token file is empty or invalid")
        return token
