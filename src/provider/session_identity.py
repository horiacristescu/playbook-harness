"""Provider-native interactive session identity contract.

This module is intentionally pure: it validates and compares identities and
constructs child environments, but never reads/writes Playbook state or starts a
process. Hook entry points and launchers must validate here before performing
those effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Mapping, Optional


NORMALIZED_SESSION_ENV = "PLAYBOOK_SESSION_ID"
_IDENTITY_ENV_NAMES = frozenset(
    {
        NORMALIZED_SESSION_ENV,
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_THREAD_ID",
        "ANTIGRAVITY_CONVERSATION_ID",
        "PLAYBOOK_BRIDGE_PROVIDER",
        "PLAYBOOK_ROLE",
    }
)
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class NativeSessionIdentityError(ValueError):
    """A provider did not supply one safe, authoritative native identity."""


@dataclass(frozen=True)
class ResolvedSessionIdentity:
    """Canonical provider plus its native interactive conversation ID."""

    provider: str
    session_id: str


@dataclass(frozen=True)
class ProviderSessionIdentitySpec:
    """Static provider dialect for obtaining one native interactive ID."""

    provider: str
    hook_source: str
    hook_field: Optional[str]
    command_env: str
    command_env_is_native: bool


@dataclass(frozen=True)
class SessionConformance:
    """Evidence-bearing declaration of interactive session readiness."""

    provider: str
    hook_source: str
    command_source: str
    exact_resume: bool
    resume_cwd: str
    supported: bool
    unsupported_reason: Optional[str] = None

    def __post_init__(self) -> None:
        spec = identity_spec(self.provider)
        if self.provider != spec.provider:
            raise ValueError(
                f"session conformance provider must use canonical name {spec.provider!r}"
            )
        if self.hook_source != spec.hook_source:
            raise ValueError("session conformance hook source disagrees with identity spec")
        if self.command_source != spec.command_env:
            raise ValueError("session conformance command source disagrees with identity spec")
        if self.supported and self.unsupported_reason:
            raise ValueError("a supported provider cannot have an unsupported reason")
        if not self.supported and not self.unsupported_reason:
            raise ValueError("an unsupported provider must explain why")


_SPECS = {
    "claude": ProviderSessionIdentitySpec(
        provider="claude",
        hook_source="hook payload session_id",
        hook_field="session_id",
        command_env="CLAUDE_CODE_SESSION_ID",
        command_env_is_native=True,
    ),
    "codex": ProviderSessionIdentitySpec(
        provider="codex",
        hook_source="hook payload session_id",
        hook_field="session_id",
        command_env="CODEX_THREAD_ID",
        command_env_is_native=True,
    ),
    "antigravity": ProviderSessionIdentitySpec(
        provider="antigravity",
        hook_source="hook payload conversationId",
        hook_field="conversationId",
        command_env="ANTIGRAVITY_CONVERSATION_ID",
        command_env_is_native=True,
    ),
    "pi": ProviderSessionIdentitySpec(
        provider="pi",
        hook_source="ctx.sessionManager.getSessionId()",
        hook_field=None,
        command_env=NORMALIZED_SESSION_ENV,
        command_env_is_native=False,
    ),
    "omp": ProviderSessionIdentitySpec(
        provider="omp",
        hook_source="ctx.sessionManager.getSessionId()",
        hook_field=None,
        command_env=NORMALIZED_SESSION_ENV,
        command_env_is_native=False,
    ),
}
_ALIASES = {"agy": "antigravity"}


def identity_spec(provider: str) -> ProviderSessionIdentitySpec:
    """Return the provider dialect or reject an undeclared provider."""

    canonical = _ALIASES.get(provider, provider)
    try:
        return _SPECS[canonical]
    except KeyError as exc:
        raise NativeSessionIdentityError(
            f"provider {provider!r} has no interactive session identity contract"
        ) from exc


def declared_session_conformance(
    provider: str,
    *,
    exact_resume: bool,
    resume_cwd: str,
    supported: bool,
    unsupported_reason: Optional[str] = None,
) -> SessionConformance:
    """Build a conformance result from the provider's executable identity spec."""

    spec = identity_spec(provider)
    return SessionConformance(
        provider=spec.provider,
        hook_source=spec.hook_source,
        command_source=spec.command_env,
        exact_resume=exact_resume,
        resume_cwd=resume_cwd,
        supported=supported,
        unsupported_reason=unsupported_reason,
    )


def validate_native_session_id(value: object) -> str:
    """Return *value* unchanged when it is a safe native ID.

    Native IDs become path components and environment values. The accepted
    alphabet covers the observed UUID/token forms while excluding traversal,
    whitespace, control bytes, and shell syntax before any effect occurs.
    """

    if not isinstance(value, str) or not _SESSION_ID_RE.fullmatch(value):
        raise NativeSessionIdentityError("missing or malformed native session ID")
    return value


def hook_session_id(
    provider: str,
    payload: Mapping[str, object],
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """Extract the hook ID and reject a disagreeing command observation."""

    spec = identity_spec(provider)
    if spec.hook_field is None:
        raise NativeSessionIdentityError(
            f"{spec.provider} identity comes from {spec.hook_source}, not hook JSON"
        )
    hook_id = validate_native_session_id(payload.get(spec.hook_field))
    if environ is not None and environ.get(spec.command_env):
        require_matching_session_id(hook_id, command_session_id(provider, environ))
    return hook_id


def command_session_id(provider: str, environ: Mapping[str, str]) -> str:
    """Extract and validate the ID visible to an agent-run command."""

    spec = identity_spec(provider)
    if (
        not spec.command_env_is_native
        and environ.get("PLAYBOOK_BRIDGE_PROVIDER") != spec.provider
    ):
        raise NativeSessionIdentityError(
            f"{spec.provider} normalized identity requires its declared bridge"
        )
    return validate_native_session_id(environ.get(spec.command_env))


def resolve_command_session_id(
    environ: Mapping[str, str], provider: Optional[str] = None
) -> str:
    """Resolve a command identity without PID, cwd, or recency guesses.

    A provider hint selects that provider's native command source. Without a
    hint, exactly one native provider variable may be present. The normalized
    transport is accepted only from the declared Pi/OMP bridge or an explicit
    noninteractive execution. An arbitrary ambient PLAYBOOK_SESSION_ID is never
    interactive authority.
    """

    native_values = {
        name: validate_native_session_id(environ[name])
        for name in (
            "CLAUDE_CODE_SESSION_ID",
            "CODEX_THREAD_ID",
            "ANTIGRAVITY_CONVERSATION_ID",
        )
        if environ.get(name)
    }
    distinct = set(native_values.values())
    if len(distinct) > 1:
        raise NativeSessionIdentityError(
            "multiple provider-native session IDs disagree"
        )

    hint = provider or environ.get("PLAYBOOK_PROVIDER")
    if hint:
        spec = identity_spec(hint)
        if not spec.command_env_is_native and native_values:
            raise NativeSessionIdentityError(
                "foreign provider-native session ID leaked into adapter transport"
            )
        if (
            not spec.command_env_is_native
            and environ.get("PLAYBOOK_BRIDGE_PROVIDER") != spec.provider
        ):
            raise NativeSessionIdentityError(
                f"{spec.provider} normalized identity requires its declared bridge"
            )
        return validate_native_session_id(environ.get(spec.command_env))

    if len(distinct) == 1:
        return next(iter(distinct))

    if environ.get("PLAYBOOK_ROLE") == "noninteractive":
        return validate_native_session_id(environ.get(NORMALIZED_SESSION_ENV))
    raise NativeSessionIdentityError(
        "ambient PLAYBOOK_SESSION_ID is not interactive session authority"
    )


def resolve_command_session_identity(
    environ: Mapping[str, str], provider: Optional[str] = None
) -> ResolvedSessionIdentity:
    """Resolve the provider-qualified command identity without guessing.

    An explicit provider hint selects its declared transport. Otherwise one
    and only one native provider variable must identify the caller. Pi/OMP use
    their bridge marker as the provider authority for normalized transport.
    """

    hint = provider or environ.get("PLAYBOOK_PROVIDER")
    if hint:
        spec = identity_spec(hint)
        return ResolvedSessionIdentity(
            spec.provider, resolve_command_session_id(environ, spec.provider)
        )

    native = [
        (name, spec.provider)
        for spec in _SPECS.values()
        if spec.command_env_is_native
        for name in (spec.command_env,)
        if environ.get(name)
    ]
    if len(native) != 1:
        if native:
            raise NativeSessionIdentityError(
                "multiple provider-native session sources are ambiguous"
            )
        raise NativeSessionIdentityError(
            "provider-qualified interactive session identity is unavailable"
        )
    _, canonical = native[0]
    return ResolvedSessionIdentity(
        canonical, resolve_command_session_id(environ, canonical)
    )


def require_matching_session_id(hook_id: object, command_id: object) -> str:
    """Validate both observations and return their shared identity."""

    hook_value = validate_native_session_id(hook_id)
    command_value = validate_native_session_id(command_id)
    if hook_value != command_value:
        raise NativeSessionIdentityError("hook and command native session IDs disagree")
    return hook_value


def scrub_inherited_session_identity(environ: Mapping[str, str]) -> dict[str, str]:
    """Copy an environment without a parent agent's identity variables."""

    return {key: value for key, value in environ.items() if key not in _IDENTITY_ENV_NAMES}


def child_command_environment(
    environ: Mapping[str, str], native_session_id: object
) -> dict[str, str]:
    """Return an environment whose normalized transport is the native ID."""

    session_id = validate_native_session_id(native_session_id)
    result = dict(environ)
    result[NORMALIZED_SESSION_ENV] = session_id
    return result


def confined_session_dir(
    agent_dir: Path, provider: str, native_session_id: object
) -> Path:
    """Compatibility reader for the shared provider-qualified path contract."""

    from .session_state import SessionKey, SessionStateError, session_directory

    try:
        return session_directory(
            agent_dir, SessionKey.from_values(provider, native_session_id)
        )
    except SessionStateError as exc:
        raise NativeSessionIdentityError(str(exc)) from exc
