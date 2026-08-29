"""云端协调员客户端（OpenAI 兼容接口，默认智谱 glm-4-flash 免费档）

与 OllamaClient 同接口（available/chat），可互换。适合显卡跑不动本地模型时用。
API key 来源（优先级）:
  1. 环境变量 ZHIPU_API_KEY
  2. settings.coordinator.api_key_file 指定的本地文件（默认 zhipu_key.txt，已 gitignore）
"""
import os

import requests


class CloudClient:
    def __init__(
        self,
        api_key_file: str = "zhipu_key.txt",
        base_url: str = "https://open.bigmodel.cn/api/paas/v4",
        model: str = "glm-4-flash",
        env_var: str = "ZHIPU_API_KEY",
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = os.environ.get(env_var) or self._read_key_file(api_key_file)

    def _read_key_file(self, path: str) -> str:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def available(self) -> bool:
        return bool(self.api_key)

    async def chat(self, prompt: str, system: str | None = None) -> str:
        if not self.available():
            return ""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                json={"model": self.model, "messages": messages, "stream": False},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except (requests.RequestException, KeyError, IndexError):
            return ""
