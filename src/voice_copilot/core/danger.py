# ruff: noqa: RUF001
"""Supervisor+'s seatbelt: commands that are dangerous whatever the task.

Checked before a tool runs (plugin hooks) or as soon as it is seen (proxy, spawned
CLIs). Plain rules, no model: the answer has to come in milliseconds and be the
same every time. A seatbelt for the obvious cases, not a sandbox: anything these
rules don't name goes through, and the CLI's own permissions still apply.

What fires is on purpose narrower than "anything that deletes": `rm -rf node_modules`
or `git push --force-with-lease` is the agent doing its job, and in the stop-and-wait
reaction a false alarm halts the agent. A command quoted inside a search
(`grep "git push --force"`) is text, not a command, and doesn't fire either.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from voice_copilot.proxy.tool_events import file_path_from_tool

#: Spoken names of what a rule caught, per reply language.
_LABELS: dict[str, dict[str, str]] = {
    "delete": {
        "en": "a recursive delete outside the project's own folders",
        "ru": "рекурсивное удаление за пределами папок проекта",
        "uk": "рекурсивне видалення за межами тек проєкту",
        "es": "un borrado recursivo fuera de las carpetas del proyecto",
        "fr": "une suppression récursive hors des dossiers du projet",
    },
    "discard": {
        "en": "throwing away uncommitted changes",
        "ru": "сброс незакоммиченных изменений",
        "uk": "скидання незакомічених змін",
        "es": "descartar cambios sin commit",
        "fr": "l'abandon des modifications non commitées",
    },
    "clean": {
        "en": "deleting untracked files",
        "ru": "удаление неотслеживаемых файлов",
        "uk": "видалення невідстежуваних файлів",
        "es": "borrar archivos sin seguimiento",
        "fr": "la suppression des fichiers non suivis",
    },
    "force_push": {
        "en": "a force push",
        "ru": "принудительный push",
        "uk": "примусовий push",
        "es": "un push forzado",
        "fr": "un push forcé",
    },
    "branch_delete": {
        "en": "deleting a branch",
        "ru": "удаление ветки",
        "uk": "видалення гілки",
        "es": "borrar una rama",
        "fr": "la suppression d'une branche",
    },
    "no_verify": {
        "en": "skipping git hooks with no-verify",
        "ru": "пропуск git-хуков через no-verify",
        "uk": "пропуск git-хуків через no-verify",
        "es": "saltarse los hooks de git con no-verify",
        "fr": "le contournement des hooks git avec no-verify",
    },
    "history": {
        "en": "rewriting git history",
        "ru": "переписывание истории git",
        "uk": "переписування історії git",
        "es": "reescribir el historial de git",
        "fr": "la réécriture de l'historique git",
    },
    "database": {
        "en": "dropping database data",
        "ru": "удаление данных в базе",
        "uk": "видалення даних у базі",
        "es": "borrar datos de la base",
        "fr": "la suppression de données en base",
    },
    "remote_script": {
        "en": "running a script straight from the internet",
        "ru": "запуск скрипта прямо из интернета",
        "uk": "запуск скрипта просто з інтернету",
        "es": "ejecutar un script directamente de internet",
        "fr": "l'exécution d'un script venu d'internet",
    },
    "permissions": {
        "en": "opening permissions to everyone",
        "ru": "открытие прав для всех",
        "uk": "відкриття прав для всіх",
        "es": "abrir permisos a todos",
        "fr": "l'ouverture des droits à tous",
    },
    "secrets": {
        "en": "writing to a file with secrets",
        "ru": "запись в файл с секретами",
        "uk": "запис у файл із секретами",
        "es": "escribir en un archivo de secretos",
        "fr": "l'écriture dans un fichier de secrets",
    },
    "publish": {
        "en": "publishing a package",
        "ru": "публикация пакета",
        "uk": "публікація пакета",
        "es": "publicar un paquete",
        "fr": "la publication d'un paquet",
    },
    "disk": {
        "en": "writing to a whole disk",
        "ru": "запись прямо на диск",
        "uk": "запис прямо на диск",
        "es": "escribir directamente en un disco",
        "fr": "l'écriture directe sur un disque",
    },
}

RULES = frozenset(_LABELS)


@dataclass(frozen=True)
class Danger:
    #: Which rule fired: a key of `RULES`.
    rule: str
    #: The command or path that fired it, cut short, for the panel and the agent.
    excerpt: str

    def label(self, language: str) -> str:
        names = _LABELS[self.rule]
        return names.get(language) or names["en"]


_EXCERPT = 160
_SHELL_KEYS = ("command", "cmd", "script")
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "pwsh", "powershell", "cmd"}
#: Words that run the rest of the line as a command.
_PREFIXES = {"sudo", "env", "nohup", "time", "command", "exec", "xargs", "doas", "nice"}
_EDIT_TOOLS = {
    "edit",
    "write",
    "multiedit",
    "notebookedit",
    "create_file",
    "update_file",
    "write_file",
    "edit_file",
    "apply_patch",
    "str_replace_editor",
    "str_replace_based_edit_tool",
}
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
_SQL_CLIENTS = {"psql", "mysql", "mariadb", "sqlite3", "sqlcmd", "clickhouse-client", "mongosh"}
_SQL_DROP = re.compile(r"\b(?:drop\s+(?:table|database|schema)|truncate)\b", re.IGNORECASE)
_DOWNLOADERS = {"curl", "wget", "iwr", "irm", "invoke-webrequest", "invoke-restmethod"}
_RUNNERS = {
    "sh",
    "bash",
    "zsh",
    "python",
    "python3",
    "node",
    "iex",
    "invoke-expression",
    "pwsh",
    "powershell",
}
_PUBLISHERS = {
    ("npm", "publish"),
    ("pnpm", "publish"),
    ("yarn", "publish"),
    ("twine", "upload"),
    ("cargo", "publish"),
    ("uv", "publish"),
    ("poetry", "publish"),
    ("gem", "push"),
    ("flit", "publish"),
    ("hatch", "publish"),
}
_REDIRECT = re.compile(r"(?:>>?|\btee\s+(?:-a\s+)?)\s*[\"']?(?P<path>[^\s\"'|;&<>]+)")
_TEMP = ("/tmp/", "/var/tmp/", "/appdata/local/temp/", "$tmpdir/", "%temp%/", "$env:temp/")
_HARMLESS_DEVICES = {"of=/dev/null", "of=/dev/zero", "of=/dev/stdout", "of=/dev/stderr"}
#: `<<EOF`, `<<'EOF'`, `<<-"EOF"`: what follows up to the terminator is text, not commands.
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_ALWAYS_DANGEROUS_TARGETS = {
    "/",
    "/*",
    "~",
    "~/",
    "~/*",
    "*",
    ".",
    "./",
    "./*",
    "..",
    "../",
    "../*",
    "$home",
    "${home}",
    "%userprofile%",
    "$env:userprofile",
    ".git",
    "./.git",
}


def check_tool(tool: str | None, tool_input: Any, cwd: str | None = None) -> Danger | None:
    """The rule this tool call breaks, or None if it is fine as far as these rules go."""
    command = _command_text(tool_input)
    if command is not None:
        found = check_command(command, cwd)
        if found is not None:
            return found
    if (tool or "").strip().lower() in _EDIT_TOOLS or file_path_from_tool(tool, tool_input):
        for path in _edited_paths(tool, tool_input):
            if _is_secret_file(path):
                return Danger("secrets", path[:_EXCERPT])
    return None


def check_command(command: str | list[str], cwd: str | None = None) -> Danger | None:
    """The rule this shell command breaks, or None."""
    if isinstance(command, list):
        words = [str(w) for w in command]
        if len(words) >= 3 and _program(words[0]) in _SHELLS:
            return check_command(words[-1], cwd)
        command = " ".join(words)
    return _check_line(command, cwd, depth=0)


def simple_commands(command: str | list[str]) -> list[list[str]]:
    """Each command this line runs, as words, wrappers opened: what actually executes.

    `bash -lc "vc go"` gives `["vc", "go"]`; `grep "vc go" docs` gives `["grep", ...]`.
    """
    if isinstance(command, list):
        words = [str(w) for w in command]
        if len(words) >= 3 and _program(words[0]) in _SHELLS:
            return simple_commands(words[-1])
        command = " ".join(words)
    return _simple(command, depth=0)


def _simple(line: str, depth: int) -> list[list[str]]:
    found: list[list[str]] = []
    for segment, _ in _segments(_without_heredocs(line)):
        words = _words(segment)
        while words and (_program(words[0]) in _PREFIXES or _is_assignment(words[0])):
            words = words[1:]
        if not words:
            continue
        inner = _wrapped_command(_program(words[0]), words) if depth < 3 else None
        found.extend(_simple(inner, depth + 1) if inner is not None else [words])
    return found


def program_of(words: list[str]) -> str:
    """The program a command runs, as a lower-case name without path or `.exe`."""
    return _program(words[0]) if words else ""


def _check_line(line: str, cwd: str | None, depth: int) -> Danger | None:
    previous: list[str] = []
    for segment, piped in _segments(_without_heredocs(line)):
        words = _words(segment)
        while words and (_program(words[0]) in _PREFIXES or _is_assignment(words[0])):
            words = words[1:]
        if not words:
            previous = []
            continue
        program = _program(words[0])
        inner = _wrapped_command(program, words) if depth < 3 else None
        if inner is not None:
            found = _check_line(inner, cwd, depth + 1)
            if found is not None:
                return found
        found = _check_words(program, words, segment, cwd)
        downloaded = piped and previous and _program(previous[0]) in _DOWNLOADERS
        if found is None and downloaded and program in _RUNNERS:
            found = Danger("remote_script", line)
        if (
            found is None
            and program in _RUNNERS
            and re.search(r"[$<]\(\s*(?:curl|wget|iwr|irm)\b", segment, re.IGNORECASE)
        ):
            found = Danger("remote_script", line)
        if (
            found is None
            and program in ("iex", "invoke-expression")
            and re.search(
                r"downloadstring|\b(?:iwr|irm|invoke-webrequest|invoke-restmethod)\b",
                segment,
                re.IGNORECASE,
            )
        ):
            found = Danger("remote_script", line)
        if found is not None:
            return Danger(found.rule, found.excerpt[:_EXCERPT])
        previous = words
    return None


def _check_words(program: str, words: list[str], segment: str, cwd: str | None) -> Danger | None:
    args = words[1:]
    if program == "git":
        return _check_git(args, segment)
    if program in ("rm", "remove-item", "ri", "rmdir", "rd", "del", "erase"):
        return _check_delete(program, args, segment, cwd)
    if program == "chmod" and "777" in args and any(a in ("-R", "--recursive") for a in args):
        return Danger("permissions", segment)
    if program in _SQL_CLIENTS and _SQL_DROP.search(segment):
        return Danger("database", segment)
    if program in ("dropdb", "diskpart", "fdisk") or program.startswith("mkfs"):
        return Danger("database" if program == "dropdb" else "disk", segment)
    if program == "dd" and any(
        a.lower().startswith("of=/dev/") and a.lower() not in _HARMLESS_DEVICES for a in args
    ):
        return Danger("disk", segment)
    if program == "format" and args and re.fullmatch(r"[a-zA-Z]:", args[0]):
        return Danger("disk", segment)
    sub = next((a.lower() for a in args if not a.startswith("-")), "")
    if (program, sub) in _PUBLISHERS:
        return Danger("publish", segment)
    if program == "gh" and args[:2] == ["release", "create"]:
        return Danger("publish", segment)
    for match in _REDIRECT.finditer(segment):
        if _is_secret_file(match.group("path")):
            return Danger("secrets", segment)
    return None


def _check_git(args: list[str], segment: str) -> Danger | None:
    # Skip global options: `git -C path push`, `git -c key=value commit`.
    while args and args[0].startswith("-"):
        args = args[2:] if args[0] in ("-C", "-c") else args[1:]
    if not args:
        return None
    sub, rest = args[0].lower(), args[1:]
    flags = [a for a in rest if a.startswith("-")]
    if "--no-verify" in flags and sub in ("commit", "push", "merge", "rebase"):
        return Danger("no_verify", segment)
    if sub == "reset" and "--hard" in flags:
        return Danger("discard", segment)
    if sub == "checkout" and "--" in rest and rest[rest.index("--") + 1 :][:1] in (["."], ["*"]):
        return Danger("discard", segment)
    if sub == "checkout" and rest in (["."], ["*"]):
        return Danger("discard", segment)
    if sub == "restore" and "." in rest and ("--worktree" in flags or "--staged" not in flags):
        return Danger("discard", segment)
    if sub == "clean" and any(_short_has(f, "f") or f == "--force" for f in flags):
        return Danger("clean", segment)
    if sub == "push":
        refs = [a for a in rest if not a.startswith("-")]
        if "--delete" in flags or "-d" in flags or any(r.startswith(":") for r in refs[1:]):
            return Danger("branch_delete", segment)
        if "--force" in flags or any(_short_has(f, "f") for f in flags):
            return Danger("force_push", segment)
        if any(r.startswith("+") for r in refs):
            return Danger("force_push", segment)
    if sub == "branch" and ("-D" in flags or ("--delete" in flags and "--force" in flags)):
        return Danger("branch_delete", segment)
    if sub in ("filter-branch", "filter-repo"):
        return Danger("history", segment)
    return None


def _check_delete(program: str, args: list[str], segment: str, cwd: str | None) -> Danger | None:
    lowered = [a.lower() for a in args]
    if program in ("rmdir", "rd", "del", "erase"):
        recursive = "/s" in lowered or any(a.startswith("-r") for a in lowered)
        # cmd switches are `/s`, `/q`: a slash, one letter. Anything longer is a path.
        targets = [a for a in args if not a.startswith("-") and not re.fullmatch(r"/[a-zA-Z?]", a)]
    elif program in ("remove-item", "ri"):
        recursive = any(a.startswith("-r") for a in lowered)
        targets = [a for a in args if not a.startswith("-")]
    else:  # rm, also PowerShell's alias
        flags = [a for a in args if a.startswith("-") and a != "--"]
        recursive = any(
            f.lower() in ("--recursive", "-recurse")
            or (re.fullmatch(r"-[a-zA-Z]{1,4}", f) is not None and ("r" in f or "R" in f))
            for f in flags
        )
        targets = [a for a in args if not a.startswith("-")]
    if not recursive:
        return None
    if any(_dangerous_target(_unquote(t), cwd) for t in targets):
        return Danger("delete", segment)
    return None


def _dangerous_target(target: str, cwd: str | None) -> bool:
    t = _native(target)
    low = t.lower()
    if low in _ALWAYS_DANGEROUS_TARGETS or low.rstrip("/") in _ALWAYS_DANGEROUS_TARGETS:
        return True
    if re.fullmatch(r"[a-z]:/?\*?", low):
        return True  # a drive root
    if low.endswith("/.git") or low.endswith("/.git/"):
        return True
    if low.startswith(("~", "$home", "${home}", "%userprofile%", "$env:userprofile", "..")):
        return True
    if not _is_abs(t):
        return False  # a folder inside the project: build, dist, node_modules
    for marker in _TEMP:
        at = (low + "/").find(marker)
        if at != -1:
            # A folder in the temp dir is scratch; the temp dir itself, or all of it, isn't.
            return (low + "/")[at + len(marker) :].strip("/") in ("", "*")
    if cwd:
        root = _native(cwd).lower().rstrip("/") + "/"
        return not (low + "/").startswith(root) or low.rstrip("/") + "/" == root
    return True


def _native(path: str) -> str:
    """Forward slashes, and Git Bash's `/f/project` as the `f:/project` it is."""
    p = _unquote(path).replace("\\", "/")
    drive = re.match(r"/([a-zA-Z])(/|$)", p)
    return f"{drive.group(1)}:/{p[3:]}" if drive else p


def _is_abs(path: str) -> bool:
    p = _unquote(path).replace("\\", "/")
    return p.startswith("/") or bool(re.match(r"[a-zA-Z]:/", p))


def _without_heredocs(line: str) -> str:
    """Drop heredoc bodies: a commit message that mentions `rm -rf /` isn't a command."""
    lines = line.split("\n")
    kept: list[str] = []
    ending: str | None = None
    for text in lines:
        if ending is not None:
            if text.strip() == ending:
                ending = None
            continue
        kept.append(text)
        found = _HEREDOC.search(text)
        if found:
            ending = found.group(2)
    return "\n".join(kept)


def _is_secret_file(path: str) -> bool:
    p = _unquote(path).replace("\\", "/").lower()
    name = PurePosixPath(p).name
    if name == ".env" or (
        name.startswith(".env.")
        and name.split(".", 2)[-1] not in ("example", "sample", "template", "dist", "defaults")
    ):
        return True
    if name in (".npmrc", ".pypirc", ".netrc", ".git-credentials", "credentials.json"):
        return True
    if re.fullmatch(r"id_(?:rsa|ed25519|ecdsa|dsa)", name) or name.endswith(
        (".pem", ".p12", ".pfx")
    ):
        return True
    return "/.ssh/" in "/" + p or p.endswith("/.aws/credentials") or p == ".aws/credentials"


def _edited_paths(tool: str | None, tool_input: Any) -> list[str]:
    path = file_path_from_tool(tool, tool_input)
    if path:
        return [path]
    values = tool_input.values() if isinstance(tool_input, dict) else [tool_input]
    patch = "\n".join(v for v in values if isinstance(v, str))
    return [p.strip() for p in _PATCH_FILE.findall(patch)]


def _command_text(tool_input: Any) -> str | list[str] | None:
    if isinstance(tool_input, str):
        return None
    if not isinstance(tool_input, dict):
        return None
    for key in _SHELL_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            return value
    return None


def _segments(line: str) -> list[tuple[str, bool]]:
    """Split on `;`, `&&`, `||`, `|`, `&` and newlines outside quotes.

    Each piece comes with whether a pipe led into it.
    """
    pieces: list[tuple[str, bool]] = []
    current: list[str] = []
    quote: str | None = None
    piped = False
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            current.append(ch)
        elif ch in ";\n" or ch in "&|":
            double = line[i : i + 2] in ("&&", "||")
            pieces.append(("".join(current).strip(), piped))
            current = []
            piped = ch == "|" and not double
            i += 2 if double else 1
            continue
        else:
            current.append(ch)
        i += 1
    pieces.append(("".join(current).strip(), piped))
    return [(text, p) for text, p in pieces if text]


def _words(segment: str) -> list[str]:
    try:
        raw = shlex.split(segment, posix=False)
    except ValueError:
        raw = segment.split()
    return [_unquote(w) for w in raw]


def _wrapped_command(program: str, words: list[str]) -> str | None:
    """The command a shell runs for us: `bash -lc "..."`, `pwsh -Command ...`, `cmd /c ...`."""
    if program not in _SHELLS:
        return None
    for i, word in enumerate(words[1:], 1):
        w = word.lower()
        if program == "cmd" and w in ("/c", "/k"):
            return " ".join(words[i + 1 :]) or None
        if program in ("pwsh", "powershell") and w in ("-command", "-c"):
            return " ".join(words[i + 1 :]) or None
        if program not in ("cmd", "pwsh", "powershell") and w.startswith("-") and "c" in w[1:]:
            return " ".join(words[i + 1 :]) or None
    return None


def _program(word: str) -> str:
    name = _unquote(word).replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _is_assignment(word: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word))


def _short_has(flag: str, letter: str) -> bool:
    return flag.startswith("-") and not flag.startswith("--") and letter in flag[1:]


def _unquote(word: str) -> str:
    if len(word) >= 2 and word[0] == word[-1] and word[0] in ("'", '"'):
        return word[1:-1]
    return word
