import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Persona:
    name: str
    card: dict
    style: dict
    meta: dict
    skill_text: str
    directory: Path


def load_persona(profile_dir: str | Path) -> Persona:
    directory = Path(profile_dir)
    required = ["profile.json", "persona_card.json", "style.json", "SKILL.md"]
    missing = [name for name in required if not (directory / name).exists()]
    if missing:
        raise FileNotFoundError(
            f"{directory} 缺少 {', '.join(missing)}，请先运行 ingest 和 distill"
        )
    profile = json.loads((directory / "profile.json").read_text(encoding="utf-8"))
    card = json.loads((directory / "persona_card.json").read_text(encoding="utf-8"))
    style = json.loads((directory / "style.json").read_text(encoding="utf-8"))
    skill_text = (directory / "SKILL.md").read_text(encoding="utf-8")
    return Persona(
        name=profile["name"],
        card=card,
        style=style,
        meta=profile,
        skill_text=skill_text,
        directory=directory,
    )
