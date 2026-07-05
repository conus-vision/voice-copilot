def _subscription_resolution_from_token_exchange(
    candidate: CopilotTokenCandidate,
) -> CopilotSubscriptionTokenResolution | None:
    """Exchange a reusable GitHub OAuth token for a Copilot API token."""

    try:
        payload = CopilotTokenProvider._exchange_token_sync(
            _copilot_token_exchange_headers(candidate.token)
        )
    except Exception as exc:
        logger.debug(
            "Unable to exchange Copilot OAuth token from %s via %s: %s",
            candidate.source,
            _token_exchange_url(),
            exc,
        )
        return None

    token = str(payload.get("token") or "").strip()
    if not token:
        logger.debug("Copilot token exchange from %s returned no token", candidate.source)
        return None

    return _subscription_resolution(
        token=token,
        source=f"{candidate.source}:token-exchange",
        confidence="copilot-token-exchange",
        api_url=_api_url_from_exchange_payload(payload, oauth_token=candidate.token),
    )
