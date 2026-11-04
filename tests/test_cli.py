import os

from family_assistant.__main__ import load_dotenv


def test_load_dotenv_parses_and_keeps_existing(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\nFA_A=plain\nFA_B=\"quoted value\"\nexport FA_C='single'\nFA_D=from-file\nFA_E=a=b\n",
        encoding="utf-8",
    )
    for key in ("FA_A", "FA_B", "FA_C", "FA_E"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FA_D", "from-env")

    load_dotenv(env_file)

    assert os.environ["FA_A"] == "plain"
    assert os.environ["FA_B"] == "quoted value"
    assert os.environ["FA_C"] == "single"
    assert os.environ["FA_D"] == "from-env"
    assert os.environ["FA_E"] == "a=b"


def test_load_dotenv_missing_file_is_fine(tmp_path):
    load_dotenv(tmp_path / "nope.env")
