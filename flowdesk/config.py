import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    mode: str = "demo"
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    api_key: str = field(default="", repr=False)

    @classmethod
    def load(cls):
        # 只读取 FlowDesk 自己的配置，避免意外使用父项目的密钥。
        values = {**dotenv_values(ROOT / ".env"), **os.environ}
        mode = values.get("FLOWDESK_MODE", "demo")
        if mode not in {"demo", "live"}:
            raise ValueError("FLOWDESK_MODE 只能为 demo 或 live")
        data_dir = Path(values.get("FLOWDESK_DATA_DIR", "data"))
        if not data_dir.is_absolute():
            data_dir = ROOT / data_dir
        settings = cls(
            data_dir=data_dir,
            mode=mode,
            model=values.get("FLOWDESK_MODEL", "deepseek-flash"),
            base_url=values.get("FLOWDESK_BASE_URL", "https://api.deepseek.com"),
            api_key=values.get("FLOWDESK_API_KEY", ""),
        )
        if mode == "live" and not settings.api_key:
            raise ValueError("live 模式需要在 FlowDesk/.env 中设置 FLOWDESK_API_KEY")
        return settings

