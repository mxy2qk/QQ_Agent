"""Trusted, packaged skill instructions; external source text cannot install a skill."""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("qq_agent")


class SkillMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    description: str = Field(min_length=1, max_length=500)
    version: str = Field(min_length=1, max_length=32)
    tools: list[str] = Field(min_length=1, max_length=20)


@dataclass(frozen=True)
class Skill:
    metadata: SkillMetadata
    path: Path


class SkillManager:
    def __init__(self, registry, directory=None):
        self.registry = registry
        self.skills = {}
        self.directory = Path(directory or Path(__file__).parent / "skill_specs").resolve()
        for path in sorted(self.directory.glob("*/SKILL.md")):
            if not path.resolve().is_relative_to(self.directory) or path.stat().st_size > 32000:
                raise ValueError("无效 Skill 路径或文件过大")
            header, _ = self._parts(path)
            meta = SkillMetadata.model_validate(yaml.safe_load(header))
            if meta.name in self.skills or path.parent.name != meta.name:
                raise ValueError("Skill 名称重复或目录不匹配")
            if len(meta.tools) != len(set(meta.tools)) or any(
                not registry.get(t) for t in meta.tools
            ):
                raise ValueError("Skill 工具缺失或重复：" + meta.name)
            self.skills[meta.name] = Skill(meta, path)

    @staticmethod
    def _parts(path):
        text = path.read_text(encoding="utf-8")
        match = re.fullmatch(r"---\r?\n(.*?)\r?\n---\r?\n(.*)", text, re.DOTALL)
        if not match:
            raise ValueError("Skill 缺少有效 frontmatter")
        return match.groups()

    def load(self, name):
        if name not in self.skills:
            raise ValueError("未知 Skill")
        header, body = self._parts(self.skills[name].path)
        # Reject edited metadata after discovery instead of silently elevating tools.
        if SkillMetadata.model_validate(yaml.safe_load(header)) != self.skills[name].metadata:
            raise ValueError("Skill 元数据变动，请重新加载管理器")
        return body

    def match(self, text):
        if text.startswith("/skill "):
            name = text.split(maxsplit=2)[1]
            if name not in self.skills:
                raise ValueError("未知 Skill，请使用 --list-skills 查看可用技能。")
            return [name]
        lowered = text.lower()
        names = []
        if lowered.split() and lowered.split()[0] == "/tag":
            names.append("pixiv-random")
        if any(k in lowered for k in ("贴吧", "神贴", "神帖", "tieba.baidu.com")):
            names.append("tieba-hot")
        if any(k in lowered for k in ("b站", "b 站", "哔哩", "bilibili", "bv1")):
            names.append("bilibili-hot")
        return [n for n in names if n in self.skills]

    def catalog(self):
        return "\n".join(
            f"{s.metadata.name}: {s.metadata.description}" for s in self.skills.values()
        )

    def allowed(self, names):
        return {t for n in names for t in self.skills[n].metadata.tools}

    def prompt(self, names):
        return (
            "可用技能摘要：\n"
            + self.catalog()
            + "\n"
            + "\n".join(f"已选技能 {name}：\n{self.load(name)}" for name in names)
        )
