#!/usr/bin/env python3
"""Detect upstream drift in the slice of `headroom` we borrow patterns from.

We adapt a handful of ideas from https://github.com/chopratejas/headroom
(CLI wrap / proxy lifecycle / Copilot auth) — see ``docs/headroom-porting.md``.
`headroom` is a large, fast-moving repo whose files churn for reasons that don't
concern us, so diffing whole files is pure noise. Instead we track a *watch list*
of specific top-level symbols (functions / classes / constants) and snapshot each
one's source at the version we last reviewed. This tool re-extracts those same
symbols from a newer checkout and shows exactly which ones changed, so porting an
upstream improvement is "re-review these 3 functions", not "re-read 6k lines".

Usage:
    # Compare a fresh headroom checkout against our snapshots (default: tmp/headroom-main)
    python scripts/check_headroom_updates.py --headroom-dir path/to/headroom

    # Re-baseline after reviewing/porting a change (writes new snapshots)
    python scripts/check_headroom_updates.py --headroom-dir path/to/headroom --update

Exit code is non-zero when drift or a missing symbol is found (CI-friendly).
"""

from __future__ import annotations

import argparse
import ast
import difflib
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SNAPSHOT_DIR = _REPO_ROOT / "docs" / "headroom-porting" / "snapshots"
_DEFAULT_HEADROOM_DIR = _REPO_ROOT / "tmp" / "headroom-main"

# Pinned upstream version these snapshots were taken from. Bump when re-baselining.
HEADROOM_VERSION = "0.29.0"


@dataclass(frozen=True)
class Watch:
    """One upstream symbol we track."""

    group: str  # why we care (matches a section in docs/headroom-porting.md)
    rel_path: str  # path within the headroom repo
    symbol: str  # top-level function / class / constant name

    @property
    def snapshot_name(self) -> str:
        stem = self.rel_path.replace("/", "__").removesuffix(".py")
        return f"{stem}__{self.symbol}.py"


# The watch list. Grouped by the backlog item each symbol backs; keep in sync
# with docs/headroom-porting.md.
WATCHES: tuple[Watch, ...] = (
    # --- Group A: patterns we already adopted (watch for upstream improvements)
    Watch("tool-search", "headroom/cli/wrap.py", "_configure_tool_search_env"),
    Watch("tool-search", "headroom/cli/wrap.py", "_normalize_tool_search_mode"),
    Watch("remote-control", "headroom/providers/claude/runtime.py", "remote_control_gate_message"),
    Watch(
        "remote-control", "headroom/providers/claude/runtime.py", "REMOTE_CONTROL_DISABLED_MESSAGE"
    ),
    Watch("tool-search", "headroom/providers/claude/runtime.py", "TOOL_SEARCH_DEFAULT"),
    # --- Group B: shared-proxy resilience on Windows (backlog)
    Watch("shared-proxy", "headroom/cli/wrap.py", "_start_proxy"),
    Watch("shared-proxy", "headroom/cli/wrap.py", "_make_cleanup"),
    Watch("shared-proxy", "headroom/cli/wrap.py", "_live_proxy_clients"),
    Watch("shared-proxy", "headroom/cli/wrap.py", "_register_proxy_client"),
    Watch("shared-proxy", "headroom/cli/wrap.py", "_marker_pid_reused"),
    Watch("shared-proxy", "headroom/cli/wrap.py", "_proc_identity"),
    # --- Group C: Copilot CLI auth-reuse (backlog)
    Watch("copilot-auth", "headroom/copilot_auth.py", "iter_oauth_token_candidates"),
    Watch(
        "copilot-auth", "headroom/copilot_auth.py", "_subscription_resolution_from_token_exchange"
    ),
    Watch("copilot-auth", "headroom/copilot_auth.py", "_copilot_token_exchange_headers"),
    Watch("copilot-auth", "headroom/copilot_auth.py", "DEFAULT_TOKEN_EXCHANGE_URL"),
)


def _symbol_names(node: ast.AST) -> list[str]:
    """Names a top-level statement binds (def/class name, or assignment targets)."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def extract_symbol(source: str, name: str) -> str | None:
    """Return the verbatim source of top-level `name`, or None if absent.

    Uses `ast` line spans so a function is captured exactly, independent of the
    surrounding file's churn. Leading decorators are included.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    lines = source.splitlines(keepends=True)
    for node in tree.body:
        if name not in _symbol_names(node):
            continue
        start = node.lineno
        if getattr(node, "decorator_list", None):
            start = min(d.lineno for d in node.decorator_list)
        end = getattr(node, "end_lineno", node.lineno)
        return "".join(lines[start - 1 : end])
    return None


def _read_upstream(headroom_dir: Path, watch: Watch) -> str | None:
    path = headroom_dir / watch.rel_path
    if not path.exists():
        return None
    return extract_symbol(path.read_text(encoding="utf-8"), watch.symbol)


def cmd_update(headroom_dir: Path) -> int:
    _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    written, missing = 0, []
    for watch in WATCHES:
        src = _read_upstream(headroom_dir, watch)
        if src is None:
            missing.append(watch)
            continue
        (_SNAPSHOT_DIR / watch.snapshot_name).write_text(src, encoding="utf-8")
        written += 1
    print(f"Wrote {written} snapshot(s) to {_SNAPSHOT_DIR.relative_to(_REPO_ROOT)}")
    if missing:
        print("\nCould not find these symbols upstream (fix the watch list):")
        for w in missing:
            print(f"  - {w.rel_path}::{w.symbol}")
        return 1
    return 0


def cmd_check(headroom_dir: Path) -> int:
    drifted, missing, ok = [], [], 0
    for watch in WATCHES:
        snapshot_path = _SNAPSHOT_DIR / watch.snapshot_name
        baseline = snapshot_path.read_text(encoding="utf-8") if snapshot_path.exists() else None
        current = _read_upstream(headroom_dir, watch)
        if current is None:
            missing.append(watch)
            continue
        if baseline is None:
            drifted.append((watch, "", current))  # never snapshotted → treat as new
        elif baseline != current:
            drifted.append((watch, baseline, current))
        else:
            ok += 1

    print(f"headroom watch list vs snapshots (baseline v{HEADROOM_VERSION})")
    print(f"  unchanged: {ok}/{len(WATCHES)}")

    if missing:
        print("\n!! symbols no longer found upstream (renamed/removed - review):")
        for w in missing:
            print(f"  - [{w.group}] {w.rel_path}::{w.symbol}")

    if drifted:
        print("\n!! symbols changed upstream - re-review, port if useful, then --update:")
        for watch, baseline, current in drifted:
            print(f"\n### [{watch.group}] {watch.rel_path}::{watch.symbol}")
            diff = difflib.unified_diff(
                baseline.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile=f"snapshot v{HEADROOM_VERSION}",
                tofile="upstream (current)",
            )
            sys.stdout.writelines(diff)

    if not drifted and not missing:
        print("\nOK: no drift - our borrowed patterns are still current.")
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--headroom-dir",
        type=Path,
        default=_DEFAULT_HEADROOM_DIR,
        help="Path to a headroom checkout/extract (default: tmp/headroom-main).",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Re-baseline: overwrite snapshots from --headroom-dir.",
    )
    args = parser.parse_args(argv)

    headroom_dir: Path = args.headroom_dir
    if not headroom_dir.exists():
        print(
            f"headroom checkout not found at {headroom_dir}\n"
            "Download it (git clone https://github.com/chopratejas/headroom "
            "or the release zip) and pass --headroom-dir.",
            file=sys.stderr,
        )
        return 2

    return cmd_update(headroom_dir) if args.update else cmd_check(headroom_dir)


if __name__ == "__main__":
    raise SystemExit(main())
