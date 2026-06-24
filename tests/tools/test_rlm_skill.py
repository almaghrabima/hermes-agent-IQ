from pathlib import Path

import yaml

SKILL = Path(__file__).resolve().parents[2] / "skills" / "recursive-language-model" / "SKILL.md"


def test_skill_exists_with_valid_frontmatter():
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, fm, _body = text.split("---\n", 2)
    meta = yaml.safe_load(fm)
    assert meta["name"] == "recursive-language-model"
    assert isinstance(meta["description"], str) and meta["description"].strip()


def test_skill_documents_remote_gate_and_creds():
    text = SKILL.read_text(encoding="utf-8").lower()
    assert "allow_remote_backends" in text   # security gate documented
    assert "deno" in text                     # runtime prerequisite documented
