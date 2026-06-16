"""
Bash AST safety checker — tree-sitter-bash-based command analysis.

Parses a shell command into a syntax tree and inspects it for
dangerous patterns (pipe-to-shell, recursive root deletion, chmod 777
on system paths, etc.).

Uses ``tree-sitter`` (>=0.24) + ``tree-sitter-bash`` (>=0.23).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ---- Result model -----------------------------------------------------------


class BashSafetyResult(BaseModel):
    safe: bool = True
    warnings: list[str] = Field(default_factory=list)
    dangerous_patterns: list[dict[str, str]] = Field(default_factory=list)


# ---- Public API -------------------------------------------------------------


def analyze_bash_command(command: str) -> BashSafetyResult:
    """Parse *command* with tree-sitter-bash and scan for dangerous patterns.

    Returns a ``BashSafetyResult`` with ``safe=True`` only when **no** dangerous
    pattern is detected.  The ``dangerous_patterns`` list contains one entry per
    match, each with keys ``pattern``, ``location``, and ``severity``.
    """
    tree = _parse(command)
    if tree is None:
        return BashSafetyResult(
            safe=True,
            warnings=["Unable to parse command AST — safety check skipped."],
        )

    findings: list[dict[str, str]] = []
    _walk(tree.root_node, command, findings)

    if not findings:
        return BashSafetyResult(safe=True)

    warnings = [f"{f['severity']}: {f['pattern']} — {f['location']}" for f in findings]
    return BashSafetyResult(safe=False, warnings=warnings, dangerous_patterns=findings)


# ---- Internal: tree-sitter parse --------------------------------------------


def _parse(source: str) -> Any | None:
    """Parse *source* bytes with tree-sitter-bash. Returns a Tree or None."""
    try:
        import tree_sitter
        import tree_sitter_bash
    except ImportError:
        return None

    try:
        lang = tree_sitter.Language(tree_sitter_bash.language())
        parser = tree_sitter.Parser(lang)
        return parser.parse(source.encode("utf-8"))
    except Exception:
        return None


# ---- Internal: AST walk + pattern detection ---------------------------------


def _walk(node: Any, source: str, findings: list[dict[str, str]]) -> None:
    """Recursively walk *node*, collecting dangerous patterns into *findings*."""
    _check_node(node, source, findings)
    for child in node.children:
        _walk(child, source, findings)


def _check_node(node: Any, source: str, findings: list[dict[str, str]]) -> None:
    node_type = node.type

    # -- pipeline (|) -- check for curl/wget | bash/sh
    if node_type == "pipeline":
        _check_pipe_to_shell(node, source, findings)

    # -- command --
    if node_type == "command":
        name = _command_name(node, source)
        if name is None:
            return
        _check_rm_rf_root(node, name, source, findings)
        _check_chmod_dangerous(node, name, source, findings)
        _check_sudo_dangerous(node, name, source, findings)
        _check_eval_dangerous(node, name, source, findings)

    # -- redirect --
    if node_type == "file_redirect":
        _check_dev_null_overwrite(node, source, findings)


# ---- Individual pattern checks ----------------------------------------------


def _check_pipe_to_shell(
    pipeline: Any, source: str, findings: list[dict[str, str]]
) -> None:
    """Detect ``curl … | bash`` and similar patterns."""
    children = list(pipeline.children)
    if len(children) < 2:
        return
    # Left side command
    left_name = _command_name(_first_command(children[0]), source)
    # Right side
    right_name = _command_name(_first_command(children[-1]), source)

    downloaders = {"curl", "wget", "fetch"}
    shells = {"bash", "sh", "zsh", "dash", "ksh", "fish", "python", "perl", "ruby"}

    if left_name in downloaders and right_name in shells:
        findings.append({
            "pattern": "curl_pipe_bash",
            "location": _node_range(pipeline),
            "severity": "critical",
        })


def _check_rm_rf_root(
    node: Any, name: str, source: str, findings: list[dict[str, str]]
) -> None:
    """Detect ``rm -rf /`` or ``rm -rf /*``."""
    if name != "rm":
        return
    args = _collect_words(node, source)
    has_force = any(a in ("-rf", "-fr", "--force", "-r", "-f", "--recursive") for a in args)
    if not has_force:
        return
    for arg in args:
        if arg in ("/", "/*", "/.", "/.."):
            findings.append({
                "pattern": "rm_rf_root",
                "location": _node_range(node),
                "severity": "critical",
            })
            return


def _check_chmod_dangerous(
    node: Any, name: str, source: str, findings: list[dict[str, str]]
) -> None:
    """Detect ``chmod 777`` on system paths."""
    if name != "chmod":
        return
    args = _collect_words(node, source)
    has_777 = any(a in ("777", "0777") for a in args)
    if not has_777:
        return
    system_prefixes = ("/usr/", "/bin/", "/sbin/", "/etc/", "/lib/", "/boot/")
    for arg in args:
        if arg.startswith(system_prefixes):
            findings.append({
                "pattern": "chmod_777_system",
                "location": _node_range(node),
                "severity": "high",
            })
            return


def _check_sudo_dangerous(
    node: Any, name: str, source: str, findings: list[dict[str, str]]
) -> None:
    """Flag sudo usage; more thorough when wrapping a dangerous command."""
    if name != "sudo":
        return
    # Find the inner command name
    for child in node.children:
        if child.type == "command":
            inner_name = _command_name(child, source)
            if inner_name and inner_name in ("rm", "chmod", "dd", "mkfs"):
                findings.append({
                    "pattern": "sudo_dangerous",
                    "location": _node_range(node),
                    "severity": "high",
                })
                return
    # Generic sudo — still worth noting
    findings.append({
        "pattern": "sudo",
        "location": _node_range(node),
        "severity": "medium",
    })


def _check_eval_dangerous(
    node: Any, name: str, source: str, findings: list[dict[str, str]]
) -> None:
    """Detect ``eval`` with variable references."""
    if name != "eval":
        return
    args_text = _node_text_after_name(node, source)
    if "$" in args_text:
        findings.append({
            "pattern": "eval_variable",
            "location": _node_range(node),
            "severity": "medium",
        })


def _check_dev_null_overwrite(
    redirect: Any, source: str, findings: list[dict[str, str]]
) -> None:
    """Detect redirection that overwrites a system file to /dev/null."""
    text = source[redirect.start_byte : redirect.end_byte]
    if "/dev/null" not in text:
        return

    system_prefixes = ("/etc/", "/boot/", "/usr/lib/", "/var/", "~/")
    for prefix in system_prefixes:
        if prefix in text:
            findings.append({
                "pattern": "dev_null_overwrite",
                "location": _node_range(redirect),
                "severity": "critical",
            })
            return


# ---- AST helpers ------------------------------------------------------------


def _command_name(node: Any, source: str) -> str | None:
    """Extract the command name from a *command* node."""
    for child in node.children:
        if child.type == "command_name":
            return source[child.start_byte : child.end_byte].strip()
        # Nested command (e.g. variable assignment then command)
        if child.type == "command":
            return _command_name(child, source)
        # Subshell wrapped
        if child.type == "subshell":
            inner = _first_command(child)
            if inner is not None:
                return _command_name(inner, source)
    return None


def _first_command(node: Any) -> Any | None:
    """Return the first *command* descendant of *node*."""
    if node.type == "command":
        return node
    for child in node.children:
        result = _first_command(child)
        if result is not None:
            return result
    return None


def _collect_words(node: Any, source: str) -> list[str]:
    """Collect all word/string/number tokens from *node*."""
    out: list[str] = []
    for child in node.children:
        if child.type in ("word", "string", "raw_string", "number"):
            out.append(source[child.start_byte : child.end_byte].strip("'\""))
        elif child.type == "command":
            out.extend(_collect_words(child, source))
    return out


def _node_text_after_name(node: Any, source: str) -> str:
    """Return the text of *node* after the command_name child."""
    found_name = False
    parts: list[str] = []
    for child in node.children:
        if child.type == "command_name":
            found_name = True
            continue
        if found_name:
            parts.append(source[child.start_byte : child.end_byte])
    return " ".join(parts)


def _node_range(node: Any) -> str:
    return f"({node.start_point[0]+1}:{node.start_point[1]}–{node.end_point[0]+1}:{node.end_point[1]})"
