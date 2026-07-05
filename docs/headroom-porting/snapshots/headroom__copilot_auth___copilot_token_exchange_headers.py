def _copilot_token_exchange_headers(oauth_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {oauth_token}",
        **_copilot_chat_header_defaults(),
    }
