import os
import stat
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()

import pytest
from fastapi import HTTPException

from app import main, settings


def test_saved_key_wins_over_env_and_is_private(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env-1111")
    settings.delete_api_key()
    assert settings.get_api_key() == ("sk-ant-from-env-1111", "env")
    settings.save_api_key("sk-ant-saved-key-2222")
    assert settings.get_api_key() == ("sk-ant-saved-key-2222", "saved")
    assert settings.hint("sk-ant-saved-key-2222") == "…2222"
    mode = stat.S_IMODE(os.stat(settings._PATH).st_mode)
    assert mode == 0o600
    settings.delete_api_key()
    assert settings.get_api_key()[1] == "env"


def test_runner_needs_a_key_and_follows_changes(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(main, "MOCK_LLM", False)
    monkeypatch.setattr(main, "_runner", None)
    settings.delete_api_key()
    with pytest.raises(HTTPException) as e:
        main.runner()
    assert "Settings" in e.value.detail
    settings.save_api_key("sk-ant-first-key-aaaa")
    r1 = main.runner()
    assert main.runner() is r1
    settings.save_api_key("sk-ant-second-key-bbbb")
    assert main.runner() is not r1
    settings.delete_api_key()
