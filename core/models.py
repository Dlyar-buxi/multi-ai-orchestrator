"""数据模型与会话日志"""
import json
import os
import time
from dataclasses import dataclass, field, asdict


@dataclass
class Reply:
    agent: str                      # 站点名，如 deepseek
    prompt: str
    text: str = ""
    ok: bool = False
    error: str = ""
    elapsed: float = 0.0            # 耗时（秒）
    ts: float = field(default_factory=time.time)


class SessionLog:
    """把每次交互追加写入 jsonl，便于回溯（文件管理交给 Trae/Git）"""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, obj: dict):
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def write_reply(self, r: Reply):
        self.write({"kind": "reply", **asdict(r)})
