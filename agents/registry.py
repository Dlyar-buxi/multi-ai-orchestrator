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
