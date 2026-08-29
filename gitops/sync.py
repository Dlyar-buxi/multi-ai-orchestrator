"""Git 自动同步：产出文件后 add/commit/push 到 GitHub（Trae 桌面版可随时接管）

隐私策略：只上传 git.paths 白名单里的目录/文件（默认仅 outputs/）。
绝不使用 add -A —— cookie/日志/profile 由 .gitignore + 白名单双重防护。
"""
import subprocess


class GitSync:
    def __init__(self, settings: dict):
        g = settings["git"]
        self.repo = g.get("repo_dir", ".")
        self.remote = g.get("remote", "origin")
        self.branch = g.get("branch", "main")
        self.auto_push = g.get("auto_push", True)
        self.enabled = g.get("enabled", True)
        # 白名单：只有这些路径会被 add（目录或文件通配），防止误传隐私
        self.paths: list[str] = list(g.get("paths", ["outputs/"]))

    def _git(self, *args) -> tuple[int, str]:
        p = subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        return p.returncode, (p.stdout + p.stderr).strip()

    def has_repo(self) -> bool:
        return self._git("rev-parse", "--is-inside-work-tree")[0] == 0

    def _has_changes(self) -> bool:
        # 只在白名单路径有实际变更时才提交，避免空提交
        rc, out = self._git("status", "--porcelain", "--", *self.paths)
        return rc == 0 and bool(out.strip())

    def run(self, message: str) -> bool:
        if not self.enabled or not self.has_repo() or not self.paths:
            return False
        if not self._has_changes():
            return False
        self._git("add", "--", *self.paths)
        rc, out = self._git("commit", "-m", message)
        if rc != 0:
            print(f"[git] commit 跳过: {out[:200]}")
            return False
        if self.auto_push:
            rc, out = self._git("push", self.remote, self.branch)
            if rc != 0:
                print(f"[git] push 失败: {out[:300]}")
                return False
            print(f"[git] 已推送 {self.remote}/{self.branch}")
        return True
