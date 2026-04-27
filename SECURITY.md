# Security Policy

## Supported Versions

Voice Copilot is currently in alpha. Security fixes are made on the latest public release and the `main` branch.

## Reporting a Vulnerability

Please do not open a public GitHub issue for suspected security problems.

Email: info@conus.vision

Include as much detail as you can safely share:

- affected version or commit
- operating system
- reproduction steps
- impact and affected components
- whether secrets, local files, or provider tokens may be exposed

We will acknowledge the report as soon as possible and coordinate a fix before public disclosure when appropriate.

## Secret Handling Expectations

Voice Copilot should never persist provider API keys in YAML config files or logs. Secrets should come from process environment variables, `.env` files used locally, or the OS keyring.
