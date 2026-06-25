import sys

import pytest

from voice_copilot.alias_install import ensure_vc_alias, marker_path


@pytest.fixture
def isolated_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "voice_copilot.alias_install.config_path", lambda: tmp_path / "config.yaml"
    )
    return tmp_path


def test_marker_path_is_under_the_config_dir(isolated_config_dir) -> None:
    assert marker_path().parent == isolated_config_dir


def test_installs_shim_when_vc_is_not_on_path(isolated_config_dir, tmp_path, monkeypatch) -> None:
    script_dir = tmp_path / "Scripts"
    script_dir.mkdir()
    fake_script = script_dir / ("voice-copilot.exe" if sys.platform == "win32" else "voice-copilot")
    fake_script.write_text("", encoding="utf-8")

    monkeypatch.setattr("voice_copilot.alias_install.shutil.which", lambda name: None)

    ensure_vc_alias(voice_copilot_script=fake_script)

    expected_name = "vc.cmd" if sys.platform == "win32" else "vc"
    shim = script_dir / expected_name
    assert shim.exists()
    assert marker_path().exists()


def test_does_nothing_when_vc_already_exists(isolated_config_dir, tmp_path, monkeypatch) -> None:
    script_dir = tmp_path / "Scripts"
    script_dir.mkdir()
    fake_script = script_dir / ("voice-copilot.exe" if sys.platform == "win32" else "voice-copilot")
    fake_script.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        "voice_copilot.alias_install.shutil.which",
        lambda name: r"C:\some\other\vc.exe" if sys.platform == "win32" else "/usr/local/bin/vc",
    )

    ensure_vc_alias(voice_copilot_script=fake_script)

    expected_name = "vc.cmd" if sys.platform == "win32" else "vc"
    assert not (script_dir / expected_name).exists()
    assert marker_path().exists()


def test_is_a_noop_on_second_call(isolated_config_dir, tmp_path, monkeypatch) -> None:
    script_dir = tmp_path / "Scripts"
    script_dir.mkdir()
    fake_script = script_dir / ("voice-copilot.exe" if sys.platform == "win32" else "voice-copilot")
    fake_script.write_text("", encoding="utf-8")

    which_calls = []
    monkeypatch.setattr(
        "voice_copilot.alias_install.shutil.which",
        lambda name: which_calls.append(name) or None,
    )

    ensure_vc_alias(voice_copilot_script=fake_script)
    ensure_vc_alias(voice_copilot_script=fake_script)

    assert which_calls == ["vc"]


def test_never_raises_on_unwritable_directory(isolated_config_dir, tmp_path, monkeypatch) -> None:
    script_dir = tmp_path / "Scripts"
    script_dir.mkdir()
    fake_script = script_dir / ("voice-copilot.exe" if sys.platform == "win32" else "voice-copilot")
    fake_script.write_text("", encoding="utf-8")

    monkeypatch.setattr("voice_copilot.alias_install.shutil.which", lambda name: None)

    def boom(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("voice_copilot.alias_install.Path.write_text", boom)

    ensure_vc_alias(voice_copilot_script=fake_script)  # must not raise
