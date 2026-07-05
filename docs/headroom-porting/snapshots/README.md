# headroom drift-baseline snapshots

These `*.py` files are **verbatim excerpts** of individual symbols from
[headroom](https://github.com/chopratejas/headroom) at **v0.29.0**, licensed
under **Apache-2.0** (© the headroom authors).

They are **not voice-copilot code** and are not imported or executed. They exist
only as a baseline for `scripts/check_headroom_updates.py`, which re-extracts the
same symbols from a newer headroom checkout to show what changed. They are
excluded from ruff/mypy (see `pyproject.toml`).

Regenerate with:

```bash
python scripts/check_headroom_updates.py --headroom-dir path/to/headroom --update
```

See `docs/headroom-porting.md` for the watch list and rationale.
