def iter_oauth_token_candidates() -> list[CopilotTokenCandidate]:
    """Return reusable token candidates in safest-first discovery order."""

    candidates: list[CopilotTokenCandidate] = []

    headroom_copilot_token = read_headroom_copilot_oauth_token()
    if headroom_copilot_token:
        candidates.append(
            CopilotTokenCandidate(
                token=headroom_copilot_token,
                source=f"headroom-copilot-auth:{headroom_copilot_auth_path()}",
                confidence="copilot-oauth",
            )
        )

    for env_var in _COPILOT_OAUTH_TOKEN_ENV_VARS:
        token = os.environ.get(env_var, "").strip()
        if token:
            candidates.append(
                CopilotTokenCandidate(
                    token=token,
                    source=f"env:{env_var}",
                    confidence="explicit",
                )
            )

    windows_copilot_token = _read_windows_copilot_cli_oauth_token()
    if windows_copilot_token:
        candidates.append(
            CopilotTokenCandidate(
                token=windows_copilot_token,
                source="windows-credential-manager:copilot-cli",
                confidence="high",
            )
        )

    macos_copilot_token = _read_macos_keychain_oauth_token()
    if macos_copilot_token:
        candidates.append(
            CopilotTokenCandidate(
                token=macos_copilot_token,
                source="macos-keychain:copilot-cli",
                confidence="high",
            )
        )

    linux_copilot_token = _read_linux_secret_oauth_token()
    if linux_copilot_token:
        candidates.append(
            CopilotTokenCandidate(
                token=linux_copilot_token,
                source="linux-secret-service:copilot-cli",
                confidence="high",
            )
        )

    candidates.extend(_read_file_oauth_token_candidates())

    for env_var in _GENERIC_GITHUB_TOKEN_ENV_VARS:
        token = os.environ.get(env_var, "").strip()
        if token:
            candidates.append(
                CopilotTokenCandidate(
                    token=token,
                    source=f"env:{env_var}",
                    confidence="generic-github",
                )
            )

    gh_token = _read_gh_cli_oauth_token()
    if gh_token:
        candidates.append(
            CopilotTokenCandidate(
                token=gh_token,
                source="gh-cli",
                confidence="generic-github",
            )
        )

    return _dedupe_token_candidates(candidates)
