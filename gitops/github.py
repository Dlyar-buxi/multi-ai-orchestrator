"""GitHub REST API 客户端：新建仓库等能力

token 来源（优先级）:
  1. 环境变量 GITHUB_TOKEN
  2. settings.github.token_file 指定的本地文件（默认 github_token.txt，已被 .gitignore 禁入）

生成 token: https://github.com/settings/tokens?type=beta (fine-grained)
  Repository access: All repositories 或按需指定
  Permissions: Administration -> Read and write（创建仓库所需）
"""
import json
import os
import urllib.error
import urllib.request


class GitHubError(Exception):
    pass


class GitHubClient:
    API = "https://api.github.com"

    def __init__(self, settings: dict):
        gh = settings.get("github", {}) if isinstance(settings, dict) else {}
        self.token_file = gh.get("token_file", "github_token.txt")
        self.token = os.environ.get("GITHUB_TOKEN") or self._read_token_file()

    def _read_token_file(self) -> str:
        if not os.path.exists(self.token_file):
            return ""
        with open(self.token_file, encoding="utf-8") as f:
            return f.read().strip()

    @property
    def ready(self) -> bool:
        return bool(self.token)

    def _request(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.API + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "multi-ai-orchestrator",
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            raise GitHubError(f"HTTP {e.code}: {detail}") from e

    def me(self) -> dict:
        return self._request("/user")

    def create_repo(
        self,
        name: str,
        private: bool = True,
        description: str = "",
        auto_init: bool = False,
    ) -> dict:
        """创建仓库。默认 private（隐私优先）。返回 API 响应（含 html_url/clone_url）"""
        if not self.ready:
            raise GitHubError(
                f"缺少 token：设置环境变量 GITHUB_TOKEN，或把 PAT 写入 {self.token_file}"
            )
        return self._request(
            "/user/repos",
            "POST",
            {
                "name": name,
                "private": private,
                "description": description or None,
                "auto_init": auto_init,
            },
        )

    def repo_exists(self, full_name: str) -> bool:
        try:
            self._request(f"/repos/{full_name}")
            return True
        except GitHubError:
            return False
