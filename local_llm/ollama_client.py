"""本地 Ollama 客户端：任务分解、回复仲裁、断网兜底"""
import requests


class OllamaClient:
    def __init__(self, base_url: str, model: str, enabled: bool = True):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.enabled = enabled

    def available(self) -> bool:
        if not self.enabled:
            return False
        try:
            return requests.get(f"{self.base_url}/api/tags", timeout=2).ok
        except requests.RequestException:
            return False

    async def chat(self, prompt: str, system: str | None = None) -> str:
        """流式接口按整包读取，够用且简单"""
        payload = {"model": self.model, "stream": False, "messages": []}
        if system:
            payload["messages"].append({"role": "system", "content": system})
        payload["messages"].append({"role": "user", "content": prompt})
        try:
            resp = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=600)
            resp.raise_for_status()
            return resp.json().get("message", {}).get("content", "").strip()
        except requests.RequestException:
            return ""
