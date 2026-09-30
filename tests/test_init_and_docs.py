"""rpt init, plus checks that the shipped examples and docs stay honest."""

import re
import subprocess
from pathlib import Path

from runpod_tools.cli import SUBCOMMANDS
from runpod_tools.commands.init import EXAMPLE_CONFIG
from runpod_tools.config import load_config
from tests.fakes import FakeTransport
from tests.test_cli import run

ROOT = Path(__file__).resolve().parents[1]


def test_init_writes_example_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert run(["init"], FakeTransport()) == 0
    written = tmp_path / "runpod-tools.toml"
    assert written.read_text() == EXAMPLE_CONFIG.read_text()
    assert "runpod-tools.toml" in capsys.readouterr().out


def test_init_refuses_to_overwrite_without_force(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runpod-tools.toml").write_text("[pod]\n")
    assert run(["init"], FakeTransport()) == 2
    assert "--force" in capsys.readouterr().err
    assert (tmp_path / "runpod-tools.toml").read_text() == "[pod]\n"
    assert run(["init", "--force"], FakeTransport()) == 0
    assert (tmp_path / "runpod-tools.toml").read_text() == EXAMPLE_CONFIG.read_text()


def test_example_config_loads_and_has_no_project_specifics(tmp_path):
    text = EXAMPLE_CONFIG.read_text()
    (tmp_path / "runpod-tools.toml").write_text(text)
    cfg = load_config(start=tmp_path, env={})
    assert cfg.pod.template_id == ""  # user must fill it in; nothing shipped
    assert cfg.pod.name_prefix == ""
    assert cfg.sync.remote_dest.startswith("/workspace/")


def test_readme_documents_every_subcommand():
    readme = (ROOT / "README.md").read_text()
    for name in SUBCOMMANDS:
        assert f"rpt {name}" in readme, f"README lacks `rpt {name}`"


def test_agents_md_exists_and_claude_md_points_to_it():
    assert (ROOT / "AGENTS.md").is_file()
    claude = ROOT / "CLAUDE.md"
    assert claude.is_symlink() and claude.resolve() == (ROOT / "AGENTS.md").resolve()


def test_shell_examples_parse():
    for script in ("examples/pod-start.sh", "examples/killswitch.sh"):
        subprocess.run(["bash", "-n", str(ROOT / script)], check=True)


FORBIDDEN = [
    # identifiers that belong to the project these tools were distilled from
    r"REDACTED", r"REDACTED", r"REDACTED", r"the-source-project", r"kmerkelbach",
    r"REDACTED", r"REDACTED", r"tinker", r"deliberative", r"OPENROUTER",
]


def test_no_project_specific_identifiers_shipped():
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in {".git", ".venv", ".superpowers", "__pycache__"} for part in path.parts):
            continue
        if path.suffix in {".pyc"} or path.name == ".DS_Store" or path == Path(__file__).resolve():
            continue
        if "docs/superpowers" in str(path):  # spec/plan may name the source project
            continue
        text = path.read_text(errors="ignore")
        for pat in FORBIDDEN:
            if re.search(pat, text, re.IGNORECASE):
                offenders.append(f"{path.relative_to(ROOT)}: {pat}")
    assert not offenders, "\n".join(offenders)
