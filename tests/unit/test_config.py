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


def test_default_config_has_narrate_only_when_focused_enabled(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg.focus.narrate_only_when_focused is True


def test_narrate_only_when_focused_round_trips_through_save_and_load(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    cfg = load_config(config_file)
    cfg.focus.narrate_only_when_focused = False
    save_config(cfg, config_file)
    reloaded = load_config(config_file)
    assert reloaded.focus.narrate_only_when_focused is False


def test_commentator_mode_defaults_to_auto(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg.commentator.mode == "auto"
    assert cfg.commentator.per_cli == {}


def test_commentator_mode_and_per_cli_round_trip(tmp_path: Path) -> None:
    from voice_copilot.core.config import CommentatorCliOverride

    config_file = tmp_path / "config.yaml"
    cfg = load_config(config_file)
    cfg.commentator.mode = "api"
    cfg.commentator.per_cli = {
        "gemini": CommentatorCliOverride(mode="api", model="gemini-2.0-flash")
    }
    save_config(cfg, config_file)
    reloaded = load_config(config_file)
    assert reloaded.commentator.mode == "api"
    assert reloaded.commentator.per_cli["gemini"].mode == "api"
    assert reloaded.commentator.per_cli["gemini"].model == "gemini-2.0-flash"


def test_codex_route_migration_runs_once(tmp_path: Path) -> None:
    # A config written before the ChatGPT route existed pins codex to `openai`
    # and is moved to `openai-chatgpt` on load...
    config_file = tmp_path / "config.yaml"
    proxy_cli_config_path(config_file).write_text(
        "profiles:\n  codex:\n    provider: openai\n    base_url_env: OPENAI_BASE_URL\n",
        encoding="utf-8",
    )
    cfg = load_config(config_file)
    assert cfg.proxy_cli.profiles["codex"].provider == "openai-chatgpt"

    # ...but a user on an API key who then picks `openai` again keeps it.
    cfg.proxy_cli.profiles["codex"].provider = "openai"
    save_config(cfg, config_file)
    assert load_config(config_file).proxy_cli.profiles["codex"].provider == "openai"


def test_crush_and_oh_my_pi_move_to_variables_they_read(tmp_path: Path) -> None:
    # Saved files keep the old catalog defaults: Crush never reads
    # OPENAI_BASE_URL, and Oh My Pi only honours ANTHROPIC_BASE_URL.
    config_file = tmp_path / "config.yaml"
    proxy_cli_config_path(config_file).write_text(
        "schema_version: 2\n"
        "profiles:\n"
        "  crush:\n    provider: openai\n    base_url_env: OPENAI_BASE_URL\n"
        "  omp:\n    provider: openai\n    base_url_env: OPENAI_BASE_URL\n"
        "  goose:\n    provider: openrouter\n    base_url_env: OPENROUTER_BASE_URL\n",
        encoding="utf-8",
    )
    profiles = load_config(config_file).proxy_cli.profiles
    assert profiles["crush"].base_url_env == "OPENAI_API_ENDPOINT"
    assert (profiles["omp"].provider, profiles["omp"].base_url_env) == (
        "anthropic",
        "ANTHROPIC_BASE_URL",
    )
    # A route the user chose is theirs.
    assert profiles["goose"].provider == "openrouter"

    # And the move happens once: going back to OPENAI_BASE_URL sticks.
    cfg = load_config(config_file)
    cfg.proxy_cli.profiles["crush"].base_url_env = "OPENAI_BASE_URL"
    save_config(cfg, config_file)
    assert load_config(config_file).proxy_cli.profiles["crush"].base_url_env == "OPENAI_BASE_URL"
