"""Unified sandbox launcher for headless and interactive agent invocations.

Single source of truth for write-containment when running any of the supported
CLI agents (claude, codex, agy, pi). Backends: macOS seatbelt (sandbox-exec) and
Linux bubblewrap (bwrap). Stdlib only.

Callers (cli.py judge dispatch, adapter run_headless_judge, bin/sandbox shim)
import from here; do not re-implement profile generation elsewhere.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# Priority order for default_agent() — first available wins.
_AGENT_ORDER: tuple[str, ...] = ("claude", "codex", "agy", "pi")

# Binary names per agent. Pi may resolve via the `omlx` launcher when `pi` itself
# is absent (omlx launches pi via os.execvpe, inheriting our sandbox).
_AGENT_BINARIES: dict[str, tuple[str, ...]] = {
    "claude": ("claude",),
    "codex": ("codex",),
    "agy": ("agy",),
    "pi": ("pi", "omlx"),
}

# Per-agent bypass-flag injection. These are appended to argv at the top level
# (i.e. before any subcommand). Codex's bypass is technically an `exec`
# subcommand flag — callers building codex argv must insert it AFTER the
# `exec` token themselves; bypass_args() returns the flag string for the map,
# the launcher routes accordingly.
_BYPASS_FLAGS: dict[str, list[str]] = {
    "claude": ["--dangerously-skip-permissions"],
    "agy": ["--dangerously-skip-permissions"],
    "codex": ["--dangerously-bypass-approvals-and-sandbox"],
    "pi": [],
}

# Home-relative directories that must be writable across all agents.
# Union of: claude state, codex state, gemini/agy transcripts, omlx server data,
# pi config, generic tool caches, macOS Library.
_HOME_RW_SUBPATHS: tuple[str, ...] = (
    ".codex",
    ".gemini",
    ".omlx",
    ".pi",
    ".cache",
    ".local",
    "Library",
)

# Friendly model aliases — collapse "agent + canonical model" into one knob.
# Loaded from JSON so agents can edit the table without a code release. Model
# strings drift between releases (claude-opus-4-7 → 4-8, gpt-5.5 → 5.6, …);
# updating `models.json` is enough.
#
# Resolution order:
#   1. Plugin default: <this_file_dir>/models.json (ships with the plugin)
#   2. Project override: <cwd>/.agent/models.json (writable from inside the
#      sandbox, so agents can pin per-project versions)
# Per-alias keys merge with the project file winning. Missing/malformed JSON
# is non-fatal — falls back to empty {} and pattern inference still works.
#
# Schema (both files):
#   {"aliases": {"<label>": ["<agent>", <model_or_null>, [<extras>...]]}}

def _parse_models_json(path: Path) -> dict[str, tuple[str, str | None, tuple[str, ...]]]:
    """Read one models.json. Returns {} on any parse/shape error."""
    import json
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    aliases = raw.get("aliases", {})
    if not isinstance(aliases, dict):
        return {}
    out: dict[str, tuple[str, str | None, tuple[str, ...]]] = {}
    for label, entry in aliases.items():
        if not isinstance(label, str) or not isinstance(entry, list) or len(entry) != 3:
            continue
        agent, model, extras = entry
        if not isinstance(agent, str):
            continue
        if model is not None and not isinstance(model, str):
            continue
        if not isinstance(extras, list) or not all(isinstance(x, str) for x in extras):
            continue
        out[label] = (agent, model, tuple(extras))
    return out


def _find_project_models_override() -> Path | None:
    """Walk up from cwd for a `.agent/models.json`. Returns first hit or None."""
    cwd = Path.cwd()
    for d in (cwd, *cwd.parents):
        candidate = d / ".agent" / "models.json"
        if candidate.is_file():
            return candidate
    return None


def _load_model_aliases() -> dict[str, tuple[str, str | None, tuple[str, ...]]]:
    """Plugin default + project override merged (project keys win)."""
    aliases: dict[str, tuple[str, str | None, tuple[str, ...]]] = {}
    default_path = Path(__file__).parent / "models.json"
    if default_path.is_file():
        aliases.update(_parse_models_json(default_path))
    project_override = _find_project_models_override()
    if project_override:
        aliases.update(_parse_models_json(project_override))
    return aliases


MODEL_ALIASES: dict[str, tuple[str, str | None, tuple[str, ...]]] = _load_model_aliases()


def resolve_model(model: str) -> tuple[str, str | None, tuple[str, ...]]:
    """Map a `--model X` value to `(agent, canonical_model_or_None, extra_args)`.

    Resolution order:
      1. Direct alias hit in MODEL_ALIASES.
      2. Pattern inference on canonical IDs:
         - `claude-*` → claude
         - `gpt-*` / `o1-*` / `o3-*` / `o4-*` → codex
         - `gemini-*` → agy
         - `vendor/model` (contains `/`) → pi via openrouter
         - `qwen*` → pi via oss
      3. No match → raise ValueError (caller must supply `--agent`).
    """
    if model in MODEL_ALIASES:
        return MODEL_ALIASES[model]
    if model.startswith("claude-"):
        return ("claude", model, ())
    if model in {"o1", "o3", "o4"} or model.startswith(("gpt-", "o1-", "o3-", "o4-")):
        return ("codex", model, ())
    if model.startswith("gemini-"):
        return ("agy", model, ())
    if "/" in model:
        return ("pi", model, ("--provider", "openrouter"))
    if model.startswith("qwen"):
        return ("pi", model, ("--provider", "oss"))
    raise ValueError(
        f"Cannot infer agent from model {model!r}. "
        f"Pass --agent explicitly, or use one of: {', '.join(MODEL_ALIASES)}"
    )


# ── Judge selection (panel-review + single judge) ────────────────────────────
# Canonical provider keys ("claude" | "codex" | "agy" | "pi") plus the synonyms
# callers may type. A judge spec is one of: "provider:variant", a bare provider,
# or an alias from models.json. Resolution returns (provider, variant_or_None);
# the CLI maps the provider string to a concrete adapter class (kept here to
# avoid importing adapters into sandbox.py — adapters import sandbox, not vice
# versa).
_PROVIDER_SYNONYMS: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "agy": "agy", "antigravity": "agy", "gemini": "agy",
    "pi": "pi", "qwen": "pi",
}


def _parse_judge_config(path: Path) -> dict:
    """Read `default_judge` / `panel` from one models.json. {} on any error."""
    import json
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict = {}
    dj = raw.get("default_judge")
    if isinstance(dj, str) and dj.strip():
        out["default_judge"] = dj.strip()
    panel = raw.get("panel")
    if isinstance(panel, list) and all(isinstance(x, str) for x in panel):
        out["panel"] = [x for x in panel if x.strip()]
    return out


def load_judge_config() -> dict:
    """Plugin default ⊕ project `.agent/models.json` (project wins per key).

    Returns {"default_judge": str | None, "panel": list[str]}.
    A project `panel` REPLACES the shipped panel (not merged) — to add claude
    back, a project lists every judge it wants. Empty/missing → {} defaults.
    """
    cfg: dict = {}
    default_path = Path(__file__).parent / "models.json"
    if default_path.is_file():
        cfg.update(_parse_judge_config(default_path))
    override = _find_project_models_override()
    if override:
        cfg.update(_parse_judge_config(override))
    return {"default_judge": cfg.get("default_judge"), "panel": cfg.get("panel", [])}


def resolve_judge_spec(name: str) -> tuple[str, str | None]:
    """Map a judge spec → (provider, variant_or_None).

    Accepts `provider:variant` (e.g. ``codex:gpt-5.3-codex``,
    ``claude:opus-4-8-1m``), a bare provider (``codex``, ``agy``…), or a
    models.json alias (``gpt``, ``opus``, ``gemini``…). Synonyms
    (antigravity/gemini→agy, qwen→pi) are normalized. Raises ValueError on an
    unknown spec.
    """
    name = name.strip()
    if not name:
        raise ValueError("empty judge spec")
    if ":" in name:
        prov, _, variant = name.partition(":")
        prov = prov.strip().lower()
        if prov not in _PROVIDER_SYNONYMS:
            raise ValueError(f"unknown provider {prov!r} in judge spec {name!r}")
        return (_PROVIDER_SYNONYMS[prov], variant.strip() or None)
    low = name.lower()
    if low in _PROVIDER_SYNONYMS:
        return (_PROVIDER_SYNONYMS[low], None)
    if name in MODEL_ALIASES:
        agent, model, _extras = MODEL_ALIASES[name]
        return (_PROVIDER_SYNONYMS.get(agent, agent), model)
    raise ValueError(
        f"unknown judge spec {name!r}. Use provider:variant, a provider "
        f"({', '.join(sorted(set(_PROVIDER_SYNONYMS.values())))}), or an alias "
        f"({', '.join(MODEL_ALIASES)})."
    )


def format_judge_output(result: subprocess.CompletedProcess) -> str:
    """Render a judge subprocess result so a failure can never masquerade as a
    clean empty review (the T139 `(no output)` bug).

    - non-empty stdout → returned verbatim (the review).
    - empty stdout + non-zero exit → `(FAILED — exit N)` + stderr tail, so the
      cause (nested-sandbox rc 71, auth, crash) shows up in judge.md.
    - empty stdout + exit 0 → genuinely empty (`(no output)`).
    """
    if result.stdout and result.stdout.strip():
        return result.stdout
    rc = result.returncode
    stderr = (result.stderr or "").strip()
    if rc != 0:
        tail = stderr[-800:] if stderr else "(no stderr captured)"
        return f"(FAILED — exit {rc})\n{tail}"
    return "(no output)"


# Top-level paths (non-home) that must be writable.
_SYSTEM_RW_PATHS: tuple[str, ...] = (
    "/tmp",
    "/private/tmp",
    "/var/folders",
    "/private/var/folders",
    "/dev",
)


@dataclass(frozen=True)
class AgentInfo:
    name: str
    binary_path: str | None  # absolute path if found, else None
    via: str | None          # "direct" | "omlx" | None


def detect_agents() -> dict[str, AgentInfo]:
    """Probe which agent CLIs are installed. Returns map agent → AgentInfo."""
    out: dict[str, AgentInfo] = {}
    for agent, binaries in _AGENT_BINARIES.items():
        found: str | None = None
        via: str | None = None
        for bin_name in binaries:
            path = shutil.which(bin_name)
            if path:
                found = path
                via = "direct" if bin_name == agent else bin_name
                break
        out[agent] = AgentInfo(name=agent, binary_path=found, via=via)
    return out


def default_agent() -> str:
    """First available agent by priority. Raises if none installed."""
    agents = detect_agents()
    for name in _AGENT_ORDER:
        if agents[name].binary_path:
            return name
    raise RuntimeError(
        "No supported agent found on PATH (looked for: "
        + ", ".join(sum(_AGENT_BINARIES.values(), ()))
        + ")"
    )


def is_sandboxed() -> bool:
    """True if already inside our sandbox — skip re-wrapping to avoid nesting.

    Sole signal: PLAYBOOK_SANDBOXED=1 in env. `run()` always exports this
    before exec'ing the child, so any nested invocation always sees it. The
    earlier cwd-prefix heuristic (`/tmp/eval-`) was dropped because it could
    skip wrapping when the launching process's cwd happened to be in the
    eval tree but the target project_root was elsewhere.
    """
    return os.environ.get("PLAYBOOK_SANDBOXED") == "1"


def bypass_args(agent: str) -> list[str]:
    """Per-agent bypass-flag injection (copy — callers may mutate)."""
    if agent not in _BYPASS_FLAGS:
        raise ValueError(f"Unknown agent: {agent!r}")
    return list(_BYPASS_FLAGS[agent])


def _normalize_rw(extra_rw: Iterable[str] | None) -> list[str]:
    if not extra_rw:
        return []
    return [str(Path(p).resolve()) for p in extra_rw]


def build_seatbelt_profile(
    project_dir: Path | str,
    git_dir: Path | str | None,
    extra_rw: Iterable[str] | None = None,
    *,
    project_writable: bool = True,
) -> str:
    """Generate a macOS seatbelt profile: allow default, deny writes except
    project_dir, system temp/dev, per-agent home subpaths, and extra_rw paths.
    Then deny .git writes within the project.

    project_writable=False is the contained "outdir" mode: the project/corpus
    becomes read-only (its write exception is dropped), so the only writable
    project-side location is whatever's passed via extra_rw (the workspace).
    Home/system paths stay writable — the agent binary needs its config/caches.
    """
    project = str(Path(project_dir).resolve())
    home = str(Path.home())
    rw_paths = _normalize_rw(extra_rw)

    require_nots: list[str] = []
    if project_writable:
        require_nots.append(f'        (require-not (subpath "{project}"))')
    for sys_path in _SYSTEM_RW_PATHS:
        require_nots.append(f'        (require-not (subpath "{sys_path}"))')
    # ~/.claude and ~/.claude.json* — regex covers both.
    require_nots.append(
        f'        (require-not (regex #"^{home}/\\.claude"))'
    )
    for sub in _HOME_RW_SUBPATHS:
        require_nots.append(
            f'        (require-not (subpath "{home}/{sub}"))'
        )
    for rw in rw_paths:
        require_nots.append(f'        (require-not (subpath "{rw}"))')

    profile_lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*",
        "    (require-all",
        *require_nots,
        "    )",
        ")",
    ]
    if git_dir:
        git_resolved = str(Path(git_dir).resolve())
        profile_lines.append(f'(deny file-write* (subpath "{git_resolved}"))')

    return "\n".join(profile_lines)


def build_bwrap_argv(
    project_dir: Path | str,
    git_dir: Path | str | None,
    target_argv: list[str],
    extra_rw: Iterable[str] | None = None,
    *,
    project_writable: bool = True,
) -> list[str]:
    """Generate the bwrap argv: read-only root, bind project + tmp + per-agent
    home subpaths read-write, bind git_dir read-only.

    project_writable=False is the contained "outdir" mode: the project/corpus is
    bound read-only; the only writable project-side location is extra_rw (the
    workspace). Home subpaths stay writable for the agent's config/caches.
    """
    project = str(Path(project_dir).resolve())
    home = Path.home()
    rw_paths = _normalize_rw(extra_rw)

    argv = ["bwrap", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev"]
    project_bind = "--bind" if project_writable else "--ro-bind"
    argv += [project_bind, project, project, "--bind", "/tmp", "/tmp"]

    if git_dir:
        git_resolved = str(Path(git_dir).resolve())
        argv += ["--ro-bind", git_resolved, git_resolved]

    # Pre-create + bind per-agent home subpaths.
    for sub in (".claude", *_HOME_RW_SUBPATHS):
        target = home / sub
        target.mkdir(parents=True, exist_ok=True)
        argv += ["--bind", str(target), str(target)]

    for rw in rw_paths:
        Path(rw).mkdir(parents=True, exist_ok=True)
        argv += ["--bind", rw, rw]

    argv += list(target_argv)
    return argv


def _compose_agent_argv(agent: str, agent_args: list[str]) -> list[str]:
    """Build the final binary argv with per-agent bypass-flag injection at the
    correct position. Codex needs its bypass AFTER the `exec` subcommand.
    """
    bypass = bypass_args(agent)
    if not bypass:
        return [agent, *agent_args]

    if agent == "codex":
        # Find `exec` token; insert bypass after it. If no `exec`, prepend.
        if "exec" in agent_args:
            idx = agent_args.index("exec") + 1
            return ["codex", *agent_args[:idx], *bypass, *agent_args[idx:]]
        return ["codex", *bypass, *agent_args]
    # claude / agy: top-level flag, prepend before user args.
    return [agent, *bypass, *agent_args]


def _git_dir_of(project_dir: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return (project_dir / result.stdout.strip()).resolve()
    except FileNotFoundError:
        pass
    return None


_SEATBELT_USABLE: bool | None = None
_NESTED_WARNED = False


# Representative probe profile. MUST contain a `(deny file-write* ...)` rule:
# macOS lets a trivial `(allow default)` profile nest inside another sandbox,
# but rejects any profile that ADDS a deny rule with rc 71 `sandbox_apply:
# Operation not permitted`. Our real profiles (build_seatbelt_profile) always
# emit deny rules, so the probe must too or it won't predict the real failure.
# The sentinel path is harmless — `/usr/bin/true` never writes there.
_SEATBELT_PROBE_PROFILE = (
    '(version 1)(allow default)'
    '(deny file-write* (subpath "/playbook-seatbelt-nesting-probe"))'
)


def _seatbelt_usable() -> bool:
    """True if `sandbox-exec` can apply a *deny-bearing* profile in this process.

    Returns False when nested inside *another* macOS sandbox (e.g. Codex's
    default Seatbelt command sandbox): `sandbox-exec` then fails at
    `sandbox_apply` with rc 71 because macOS forbids a nested sandbox adding
    write restrictions. We can't see a foreign outer sandbox via env (only our
    own PLAYBOOK_SANDBOXED), so we probe once with a representative profile
    (must mirror build_seatbelt_profile's deny rule) and cache the result.
    """
    global _SEATBELT_USABLE
    if _SEATBELT_USABLE is None:
        if platform.system() != "Darwin" or not shutil.which("sandbox-exec"):
            _SEATBELT_USABLE = False
        else:
            try:
                probe = subprocess.run(
                    ["sandbox-exec", "-p", _SEATBELT_PROBE_PROFILE, "/usr/bin/true"],
                    capture_output=True, timeout=10,
                )
                _SEATBELT_USABLE = probe.returncode == 0
            except (OSError, subprocess.SubprocessError):
                _SEATBELT_USABLE = False
    return _SEATBELT_USABLE


def _warn_nested_once() -> None:
    global _NESTED_WARNED
    if not _NESTED_WARNED:
        _NESTED_WARNED = True
        print(
            "[playbook] sandbox-exec can't apply here (nested inside another "
            "sandbox, e.g. Codex's) — running under the outer sandbox's "
            "containment instead.",
            file=sys.stderr,
        )


def _wrapped_argv(
    agent: str,
    agent_args: list[str],
    project: Path,
    extra_rw: Iterable[str] | None,
    project_writable: bool,
) -> list[str]:
    """Compose bypass-flag injection + seatbelt/bwrap wrapping into the final
    argv. Shared by run() (blocking) and popen() (streaming) so containment is
    generated in exactly one place. If already inside a sandbox (ours OR a
    foreign one we can't nest in), returns the inner argv with bypass flags only.
    """
    inner_argv = _compose_agent_argv(agent, agent_args)
    if is_sandboxed():
        return inner_argv
    if platform.system() == "Darwin" and shutil.which("sandbox-exec"):
        if _seatbelt_usable():
            git_dir = _git_dir_of(project)
            profile = build_seatbelt_profile(project, git_dir, extra_rw, project_writable=project_writable)
            return ["sandbox-exec", "-p", profile, *inner_argv]
        # Nested in a foreign sandbox (macOS forbids sandbox-exec nesting, rc 71).
        _warn_nested_once()
        return inner_argv
    if shutil.which("bwrap"):
        git_dir = _git_dir_of(project)
        return build_bwrap_argv(project, git_dir, inner_argv, extra_rw, project_writable=project_writable)
    # No sandbox primitive available — exec directly with bypass.
    return inner_argv


def _child_env(env: dict[str, str] | None) -> dict[str, str]:
    child_env = dict(os.environ) if env is None else dict(env)
    child_env["PLAYBOOK_SANDBOXED"] = "1"
    return child_env


def run(
    agent: str,
    agent_args: list[str],
    project_root: Path | str,
    extra_rw: Iterable[str] | None = None,
    env: dict[str, str] | None = None,
    capture_output: bool = False,
    check: bool = False,
    project_writable: bool = True,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run an agent under sandbox containment. Composes bypass-flag injection
    into argv, generates seatbelt/bwrap wrapping, exports PLAYBOOK_SANDBOXED=1
    in child env. If already inside a sandbox (ours OR a foreign one we can't
    nest in), skips wrapping but still injects bypass flags.
    """
    project = Path(project_root).resolve()
    child_env = _child_env(env)
    wrapped = _wrapped_argv(agent, agent_args, project, extra_rw, project_writable)

    return subprocess.run(
        wrapped,
        cwd=str(project),
        env=child_env,
        capture_output=capture_output,
        check=check,
        **kwargs,
    )


def popen(
    agent: str,
    agent_args: list[str],
    project_root: Path | str,
    extra_rw: Iterable[str] | None = None,
    env: dict[str, str] | None = None,
    project_writable: bool = True,
    **kwargs,
) -> subprocess.Popen:
    """Non-blocking variant of run() — returns a live Popen for streaming.

    Same containment/argv composition as run() (via _wrapped_argv), but the
    caller drives stdout incrementally (e.g. line-by-line stream-json → events
    for a chat sidebar). Defaults stdout to PIPE and text mode so callers can
    iterate `proc.stdout`; override via kwargs.
    """
    project = Path(project_root).resolve()
    child_env = _child_env(env)
    wrapped = _wrapped_argv(agent, agent_args, project, extra_rw, project_writable)

    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("text", True)
    return subprocess.Popen(
        wrapped,
        cwd=str(project),
        env=child_env,
        **kwargs,
    )


def _inject_model_args(
    agent: str,
    model: str | None,
    extras: tuple[str, ...],
    forwarded: list[str],
) -> list[str]:
    """Prepend `--model <id>` (per-agent shape) and extras to forwarded argv.

    Per-agent rules:
      - claude / pi: prepend `--model <id>` and extras
      - codex: insert `-m <id>` and extras AFTER the `exec` subcommand token
      - agy: prepend `--model <id>` and extras
    """
    extras_list = list(extras)
    if model is None:
        # No model flag for this agent — extras still go in.
        if agent == "codex" and "exec" in forwarded:
            idx = forwarded.index("exec") + 1
            return forwarded[:idx] + extras_list + forwarded[idx:]
        return extras_list + forwarded

    if agent == "codex":
        model_flag = ["-m", model] + extras_list
        if "exec" in forwarded:
            idx = forwarded.index("exec") + 1
            return forwarded[:idx] + model_flag + forwarded[idx:]
        return model_flag + forwarded

    # claude / pi / agy-with-model: top-level --model
    return ["--model", model] + extras_list + forwarded


def _format_agent_matrix(agents: dict[str, AgentInfo]) -> str:
    rows = []
    for name in _AGENT_ORDER:
        info = agents[name]
        if info.binary_path:
            tag = f"✓ {info.binary_path}"
            if info.via and info.via != "direct":
                tag += f" (via {info.via})"
        else:
            tag = "—"
        rows.append(f"  {name:8s} {tag}")
    return "\n".join(rows)


def _main(argv: list[str]) -> int:
    """CLI entry: python3 -m provider.sandbox [--list-agents | --print-profile |
    --agent X --] <agent-args>."""
    import argparse

    parser = argparse.ArgumentParser(prog="provider.sandbox", add_help=True)
    parser.add_argument("--agent", default=None,
                        help="Agent to launch (default: auto-detect or inferred from --model)")
    parser.add_argument("--model", default=None,
                        help="Model alias (opus/sonnet/haiku/gpt/gemini/qwen/deepseek) "
                             "OR canonical id (claude-*, gpt-*, gemini-*, vendor/model, qwen*). "
                             "Auto-picks the agent unless --agent is given.")
    parser.add_argument("--list-agents", action="store_true",
                        help="Print capability matrix and exit")
    parser.add_argument("--list-models", action="store_true",
                        help="Print model alias table and exit")
    parser.add_argument("--print-profile", action="store_true",
                        help="Print seatbelt profile to stdout and exit")
    parser.add_argument("--rw", action="append", default=[],
                        help="Extra read-write path (repeatable)")
    parser.add_argument("--project-root", default=None,
                        help="Project root (default: cwd)")
    parser.add_argument("--prompt", default=None,
                        help="Run a headless prompt via the unified subagent runner "
                             "(builds the agent's native invocation for you, instead of raw -- args)")
    parser.add_argument("--bare", action="store_true",
                        help="With --prompt: no context, the prompt is the whole mission")
    parser.add_argument("--stream", action="store_true",
                        help="With --prompt: stream output events live to stdout")
    parser.add_argument("agent_args", nargs=argparse.REMAINDER,
                        help="Args passed verbatim to the agent binary")

    args = parser.parse_args(argv)

    if args.list_agents:
        print("Sandbox agent capability matrix:")
        print(_format_agent_matrix(detect_agents()))
        return 0

    if args.list_models:
        print("Model aliases (use with `bin/sandbox --model X`):")
        for alias, (ag, model, extras) in MODEL_ALIASES.items():
            tail = f" + {' '.join(extras)}" if extras else ""
            print(f"  {alias:9s} -> --agent {ag}" + (f" --model {model}{tail}" if model else f"{tail} (no --model)"))
        print("Plus canonical patterns: claude-*, gpt-/o1-/o3-/o4-*, gemini-*, vendor/model, qwen*")
        return 0

    project = Path(args.project_root or Path.cwd()).resolve()

    if args.print_profile:
        print(build_seatbelt_profile(project, _git_dir_of(project), args.rw))
        return 0

    forwarded = list(args.agent_args)
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]

    # Resolve --model into (agent, canonical, extras). Validates against --agent.
    if args.model is not None:
        try:
            inferred_agent, canonical_model, extras = resolve_model(args.model)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2
        if args.agent and args.agent != inferred_agent:
            print(
                f"Error: --agent {args.agent!r} conflicts with --model {args.model!r} "
                f"(implies --agent {inferred_agent!r})",
                file=sys.stderr,
            )
            return 2
        agent = inferred_agent
        model_for_spec = canonical_model
        forwarded = _inject_model_args(agent, canonical_model, extras, forwarded)
    else:
        agent = args.agent or default_agent()
        model_for_spec = None

    # --prompt: route through the unified subagent runner (builds the agent's
    # native invocation via headless_argv). Raw `--` passthrough still works
    # when --prompt is absent.
    if args.prompt is not None:
        from . import subagent as _subagent
        spec = _subagent.SubagentSpec(
            agent=agent, model=model_for_spec, prompt=args.prompt, bare=args.bare,
        )
        if args.stream:
            for ev in _subagent.stream_subagent(spec, project_root=project):
                if ev.text:
                    sys.stdout.write(ev.text)
                    sys.stdout.flush()
            sys.stdout.write("\n")
            return 0
        res = _subagent.run_subagent(spec, project_root=project)
        print(res.text)
        return res.returncode

    result = run(agent, forwarded, project, extra_rw=args.rw)
    return result.returncode


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
