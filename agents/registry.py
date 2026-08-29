"""适配器注册表：加载 config/adapters/*.yaml"""
import glob
import os
import yaml


class Adapter:
    def __init__(self, cfg: dict):
        self.__dict__.update(cfg)

    @property
    def name(self) -> str:
        return self.__dict__["name"]

    @property
    def group(self) -> str:
        return self.__dict__.get("group", "normal")

    @property
    def info(self) -> str:
        """用于任务分解时的"专业方向参考"，有 info 就写，否则返回默认分类。"""
        return self.__dict__.get("info") or {
            "deepseek": "强推理·代码实现·深度思考",
            "qwen": "长文档处理·中文规划·thinking",
            "yuanbao": "产品体验设计·运营文案·专家模式",
            "kimi": "信息整合·快速响应·常识问答",
            "doubao": "产品功能规划·用户手册·中文自然工作模式",
            "chatglm": "结构化设计·接口规范·中文免费API",
            "chatgpt": "英文资源·通用综合能力",
            "claude": "长文审查·代码评审·一致性检查",
        }.get(self.name, "综合能力")


class Registry:
    def __init__(self, adapters_dir: str):
        self._adapters: dict[str, Adapter] = {}
        for path in glob.glob(os.path.join(adapters_dir, "*.yaml")):
            with open(path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            a = Adapter(cfg)
            self._adapters[a.name] = a

    def get(self, name: str) -> Adapter:
        if name not in self._adapters:
            raise KeyError(f"未找到站点适配器: {name}，可用: {list(self._adapters)}")
        return self._adapters[name]

    def names(self) -> list[str]:
        return list(self._adapters)

    def all(self) -> dict[str, Adapter]:
        return dict(self._adapters)
