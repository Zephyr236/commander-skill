"""技能库 —— 指挥官按需把技能赋予下属 agent。

用户需求原文：「还包含一个skills目录，指挥官根据skills目录中的技能也会赋予给对应的agent」

技能存在 skills/<name>/SKILL.md，与 Claude Code 自己的 skill 格式一致
（YAML frontmatter + 正文）。派发时把正文注入该 agent 的 system prompt。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .schemas import SkillSpec
from .workspace import Workspace

# 与 Claude Code 一致：frontmatter 必须从文件第一行开始
_FM_DELIM = "---"


class SkillError(RuntimeError):
    pass


@dataclass
class SkillMeta:
    name: str
    description: str
    body: str
    path: Path
    meta: dict

    @property
    def tokens_estimate(self) -> int:
        """粗略估算注入成本（字符数/4）。指挥官据此决定要不要全给。"""
        return len(self.body) // 4


def parse_skill_md(text: str) -> tuple[dict, str]:
    """拆出 YAML frontmatter 与正文。

    规则与 Claude Code 对齐：
      - 起始 --- 必须是文件第一行，否则整份文件当正文
      - YAML 解析失败时字段全空，但正文仍返回（不丢内容）
    """
    if not text.startswith(_FM_DELIM):
        return {}, text

    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FM_DELIM:
            end = i
            break

    if end is None:
        return {}, text     # 只有开头的 --- 没有收尾，当正文

    fm_raw = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1:]).lstrip("\n")

    try:
        meta = yaml.safe_load(fm_raw) or {}
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        meta = {}

    return meta, body


class SkillLibrary:
    """skills/ 目录的读取入口。"""

    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    def names(self) -> list[str]:
        d = self.ws.skills_dir
        if not d.is_dir():
            return []
        return sorted(
            p.name for p in d.iterdir()
            if p.is_dir() and (p / "SKILL.md").is_file()
        )

    def load(self, name: str) -> SkillMeta:
        path = self.ws.skill_file(name)
        if not path.is_file():
            raise SkillError(
                f"技能 {name!r} 不存在（找的是 {path}）。"
                f"已有技能: {self.names()}"
            )
        meta, body = parse_skill_md(path.read_text(encoding="utf-8"))
        return SkillMeta(
            name=str(meta.get("name") or name),
            description=str(meta.get("description") or ""),
            body=body.strip(),
            path=path,
            meta=meta,
        )

    def load_many(self, names: list[str], *, strict: bool = True) -> list[SkillMeta]:
        """装载一组技能。strict=False 时跳过缺失的（用于 agent 默认技能表）。"""
        out: list[SkillMeta] = []
        for n in names:
            try:
                out.append(self.load(n))
            except SkillError:
                if strict:
                    raise
        return out

    def to_specs(self, metas: list[SkillMeta]) -> list[SkillSpec]:
        return [
            SkillSpec(
                name=m.name,
                description=m.description,
                body=m.body,
                path=str(m.path.relative_to(self.ws.root)),
            )
            for m in metas
        ]

    def compose_system_prompt(
        self, base: str, metas: list[SkillMeta]
    ) -> str:
        """把技能正文拼进 system prompt。

        每个技能独立成节，标注来源路径，方便 agent 需要时回溯完整文件。
        """
        parts: list[str] = []
        if base.strip():
            parts.append(base.strip())

        if metas:
            parts.append("# 本次赋予你的技能\n")
            for m in metas:
                head = f"## 技能：{m.name}"
                if m.description:
                    head += f"\n> {m.description}"
                rel = self._rel(m.path)
                parts.append(f"{head}\n\n来源：`{rel}`\n\n{m.body}")

        return "\n\n---\n\n".join(parts)

    def _rel(self, p: Path) -> str:
        try:
            return p.relative_to(self.ws.root).as_posix()
        except ValueError:
            return str(p)

    def scaffold(self, name: str, description: str = "", body: str = "") -> Path:
        """新建一个技能骨架。指挥官可以自己造技能然后赋给下属。"""
        d = self.ws.skill_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        f = d / "SKILL.md"
        if f.exists():
            raise SkillError(f"技能 {name!r} 已存在: {f}")
        f.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n"
            f"{body or '# ' + name + chr(10) + chr(10) + '在此写技能正文。'}\n",
            encoding="utf-8",
        )
        return f
