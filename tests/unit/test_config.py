from pathlib import Path

from voice_copilot.core.config import Config, load_config, proxy_cli_config_path, save_config


def test_load_config_returns_defaults_for_missing_file(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.yaml")

    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8765
    assert cfg.human_language == "en"
    assert "claude" in cfg.proxy_cli.profiles


def test_load_config_migrates_legacy_language(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("language: ru\n", encoding="utf-8")

    cfg = load_config(config_file)

    assert cfg.human_language == "ru"
    assert cfg.commentator_language == "ru"


def test_save_config_splits_proxy_config(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    cfg = Config()
    cfg.server.port = 9000
    cfg.proxy_cli.working_directory = str(tmp_path)

    save_config(cfg, config_file)

    main_text = config_file.read_text(encoding="utf-8")
    proxy_text = proxy_cli_config_path(config_file).read_text(encoding="utf-8")

    assert "port: 9000" in main_text
    assert "proxy_cli" not in main_text
    assert f"working_directory: {tmp_path}" in proxy_text


def test_embedded_proxy_config_is_used_when_sidecar_is_missing(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "proxy_cli:\n"
        "  working_directory: /tmp/example\n"
        "  profiles:\n"
        "    claude:\n"
        "      provider: anthropic\n"
        "      base_url_env: ANTHROPIC_BASE_URL\n",
        encoding="utf-8",
    )

    cfg = load_config(config_file)

    assert cfg.proxy_cli.working_directory == "/tmp/example"
    assert cfg.proxy_cli.profiles["claude"].base_url_env == "ANTHROPIC_BASE_URL"
