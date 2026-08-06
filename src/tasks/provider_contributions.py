"""Pure provider-local integration descriptors consumed by project init."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Iterable

from provider.claude_hooks import render_dispatcher_hooks
from provider.codex_hooks import (
    codex_config_path,
    codex_hooks_feature_enabled,
    render_dispatcher_hooks as render_codex_dispatcher_hooks,
)

from .provider_detection import DetectionStatus, PROVIDER_SPECS, ProviderDetection
from .reconcile import (
    Contribution,
    CreateOnlyFileIntent,
    DirectoryIntent,
    KeyedListEntry,
    ManagedFileIntent,
    OperationState,
    ReconcileError,
    SharedBlockIntent,
    SharedKeyedListIntent,
    parse_managed_file,
    validate_relative_target,
)
from .template import agents_md_template, claude_md
from .core import resolve_agent_dir
from .runtime import RUNTIME_COMPAT_SCHEMA, runtime_commit


class IntegrationCapability(str, Enum):
    PENDING = "integration pending"
    FULL = "full"
    PREREQUISITE = "machine prerequisite"
    WRAPPER_REQUIRED = "wrapper required"
    GUIDANCE_ONLY = "guidance only"
    MANUAL_GUIDANCE = "manual guidance required"


@dataclass(frozen=True)
class ProviderIntegration:
    provider: str
    contribution: Contribution
    capability: IntegrationCapability
    detail: str
    warnings: tuple[str, ...] = ()

    @property
    def has_hooks(self) -> bool:
        return any(getattr(intent, "hook", False) for intent in self.contribution.intents)


_PROVIDER_ORDER = tuple(spec.name for spec in PROVIDER_SPECS)
_PROVIDER_RANK = {name: index for index, name in enumerate(_PROVIDER_ORDER)}


def integration_status(
    integration: ProviderIntegration,
    operation_states: Iterable[OperationState],
    *,
    include_hooks: bool,
    skipped_hook_status: str = "hooks preserved",
) -> str:
    """Derive one truthful, stable provider result from explicit capability."""
    if integration.capability == IntegrationCapability.MANUAL_GUIDANCE:
        return integration.capability.value
    if not include_hooks and integration.has_hooks:
        return skipped_hook_status
    if integration.capability != IntegrationCapability.FULL:
        return integration.capability.value
    states = frozenset(operation_states)
    if OperationState.CREATE in states:
        return "installed"
    if OperationState.UPDATE in states:
        return "updated"
    return "unchanged"


def skipped_hook_status(root: Path, integration: ProviderIntegration) -> str:
    """Classify hook state without changing or normalizing existing files."""
    managed = False
    user = False
    considered = False
    for intent in integration.contribution.intents:
        if not getattr(intent, "hook", False):
            continue
        try:
            target = validate_relative_target(root, intent.relative)
        except ReconcileError:
            considered = True
            user = True
            continue
        if isinstance(intent, ManagedFileIntent):
            considered = True
            if not target.is_file():
                continue
            try:
                parsed = parse_managed_file(
                    target.read_text(encoding="utf-8"), intent.marker_style
                )
            except ReconcileError:
                user = True
                continue
            if parsed is None:
                user = True
            else:
                metadata, _ = parsed
                if metadata.get("managed_by") == "playbook-harness":
                    managed = True
                else:
                    user = True
        elif isinstance(intent, SharedKeyedListIntent):
            considered = True
            if not target.is_file():
                continue
            try:
                document = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                user = True
                continue
            desired = {json.dumps(entry.value, sort_keys=True) for entry in intent.entries}
            observed: set[str] = set()
            for entry in intent.entries:
                value = document
                for part in entry.path:
                    value = value.get(part) if isinstance(value, dict) else None
                if isinstance(value, list):
                    observed.update(
                        json.dumps(item, sort_keys=True)
                        for item in value
                        if isinstance(item, dict)
                    )
            managed |= bool(desired & observed)
            user |= bool(observed - desired)
    if managed and user:
        return "existing managed and user hooks left unchanged"
    if managed:
        return "existing managed hooks left unchanged"
    if user:
        return "existing user hooks left unchanged"
    if considered:
        return "hooks absent and skipped"
    return "no project hooks available"


def _standalone_guidance(content: str) -> str:
    """Render provider guidance against stable machine-level dispatchers."""
    return content.replace(".claude/bin/tasks", "pb-tasks").replace(
        ".claude/bin/sandbox", "pb-sandbox"
    )


def _source_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _guidance_intents(
    root: Path,
    filename: str,
    content: str,
    provider: str,
) -> tuple[tuple[CreateOnlyFileIntent | ManagedFileIntent, ...], bool]:
    """Create missing guidance or propose it without rewriting user Markdown."""
    target = root / filename
    base: tuple[CreateOnlyFileIntent | ManagedFileIntent, ...] = (
        CreateOnlyFileIntent(filename, content),
    )
    if not target.is_file():
        return base, False
    existing = target.read_text(encoding="utf-8", errors="replace")
    # Do not let the generic suffix in legacy commands such as
    # `.claude/bin/tasks bootstrap` masquerade as current standalone guidance.
    # User-authored Markdown remains untouched; reconciliation adds a managed
    # proposal containing the current pb-* commands instead.
    incorporated = (
        "pb-tasks bootstrap" in existing and "pb-tasks work" in existing
    )
    if incorporated:
        return base, False
    proposal_name = filename.removesuffix(".md").lower()
    proposal = ManagedFileIntent(
        f".agent/templates/{proposal_name}-playbook-proposal.md",
        content,
        f"templates/{filename}",
        marker_style="markdown",
        adopt_hashes=(_source_digest(content),),
    )
    return (*base, proposal), True


def _guidance_capability(
    capability: IntegrationCapability,
    detail: str,
    proposal_required: bool,
) -> tuple[IntegrationCapability, str]:
    if not proposal_required:
        return capability, detail
    return (
        IntegrationCapability.MANUAL_GUIDANCE,
        f"incorporate the generated .agent/templates proposal; {detail}",
    )


def _runtime_asset_root(public_name: str, development_name: str) -> Path:
    runtime = Path(__file__).resolve().parents[2]
    development = runtime / development_name
    return development if development.is_dir() else runtime / public_name


def _managed_markdown_tree(
    source_root: Path, destination: str
) -> tuple[ManagedFileIntent, ...]:
    if not source_root.is_dir():
        raise ReconcileError(f"installed provider asset directory is missing: {source_root}")
    intents = []
    for source in sorted(source_root.rglob("*.md")):
        body = source.read_text(encoding="utf-8")
        relative_source = source.relative_to(source_root).as_posix()
        intents.append(
            ManagedFileIntent(
                f"{destination}/{relative_source}",
                body,
                f"{source_root.name}/{relative_source}",
                marker_style="markdown-frontmatter",
                adopt_hashes=(_source_digest(body),),
            )
        )
    if not intents:
        raise ReconcileError(f"installed provider asset directory is empty: {source_root}")
    return tuple(intents)


def _claude_integration(root: Path, guidance: str) -> ProviderIntegration:
    hook_entries = []
    for event, entries in render_dispatcher_hooks()["hooks"].items():
        hook_entries.extend(
            KeyedListEntry(
                ("hooks", event),
                ("matcher",),
                entry,
                nested_list_field="hooks",
                nested_key_fields=("command",),
            )
            for entry in entries
        )
    guidance_intents, proposal_required = _guidance_intents(
        root, "CLAUDE.md", guidance, "claude"
    )
    intents = [
        *guidance_intents,
        SharedBlockIntent(
            ".claude/.gitignore",
            "settings.local.json\n",
            "provider/claude:local-settings-ignore",
            hook=True,
        ),
        SharedKeyedListIntent(
            ".claude/settings.local.json", tuple(hook_entries), hook=True
        ),
    ]
    intents.extend(
        _managed_markdown_tree(
            _runtime_asset_root("skills", ".claude/skills"), ".claude/skills"
        )
    )
    intents.extend(
        _managed_markdown_tree(
            _runtime_asset_root("commands", ".claude/commands"), ".claude/commands"
        )
    )
    warnings = []
    shared_settings = root / ".claude" / "settings.json"
    if shared_settings.is_file():
        text = shared_settings.read_text(encoding="utf-8", errors="replace")
        if '"hooks"' in text and (
            "PLUGIN_ROOT" in text or "playbook" in text.lower()
        ):
            warnings.append(
                "possible duplicate legacy Playbook hooks in .claude/settings.json"
            )
    capability, detail = _guidance_capability(
        IntegrationCapability.FULL,
        "project-local settings, skills, and commands",
        proposal_required,
    )
    return ProviderIntegration(
        "claude",
        Contribution("claude", tuple(intents)),
        capability,
        detail,
        tuple(warnings),
    )


def _codex_integration(
    root: Path, guidance: str, feature_enabled: bool | None = None
) -> ProviderIntegration:
    hook_entries = []
    for event, entries in render_codex_dispatcher_hooks()["hooks"].items():
        hook_entries.extend(
            KeyedListEntry(
                ("hooks", event),
                ("matcher",),
                entry,
                nested_list_field="hooks",
                nested_key_fields=("command",),
            )
            for entry in entries
        )
    enabled = (
        codex_hooks_feature_enabled(codex_config_path())
        if feature_enabled is None
        else feature_enabled
    )
    capability = (
        IntegrationCapability.FULL if enabled else IntegrationCapability.PREREQUISITE
    )
    detail = (
        "project hooks enabled"
        if enabled
        else "Codex [features] hooks = true is required; init did not change it"
    )
    guidance_intents, proposal_required = _guidance_intents(
        root, "AGENTS.md", guidance, "codex"
    )
    capability, detail = _guidance_capability(
        capability, detail, proposal_required
    )
    return ProviderIntegration(
        "codex",
        Contribution(
            "codex",
            (
                *guidance_intents,
                SharedKeyedListIntent(
                    ".codex/hooks.json", tuple(hook_entries), hook=True
                ),
            ),
        ),
        capability,
        detail,
    )


def _pi_integration(root: Path, guidance: str) -> ProviderIntegration:
    runtime = Path(__file__).resolve().parents[2]
    source = runtime / "scripts" / "playbook-pi-omlx-models.json"
    if not source.is_file():
        raise ReconcileError(f"installed Pi models asset is missing: {source}")
    body = source.read_text(encoding="utf-8")
    agent_relative = resolve_agent_dir(root).relative_to(root).as_posix()
    pi_relative = f"{agent_relative}/pi"
    guidance_intents, proposal_required = _guidance_intents(
        root, "AGENTS.md", guidance, "pi"
    )
    capability, detail = _guidance_capability(
        IntegrationCapability.WRAPPER_REQUIRED,
        "launch with pb-pi so Pi loads the Playbook extension and isolated config",
        proposal_required,
    )
    return ProviderIntegration(
        "pi",
        Contribution(
            "pi",
            (
                *guidance_intents,
                DirectoryIntent(f"{pi_relative}/config", hook=True),
                DirectoryIntent(f"{pi_relative}/sessions", hook=True),
                ManagedFileIntent(
                    f"{pi_relative}/config/models.json",
                    body,
                    "scripts/playbook-pi-omlx-models.json",
                    marker_style="json",
                    hook=True,
                    adopt_hashes=(_source_digest(body),),
                ),
            ),
        ),
        capability,
        detail,
    )


def _omp_integration(root: Path, guidance: str) -> ProviderIntegration:
    runtime = Path(__file__).resolve().parents[2]
    source = runtime / "scripts" / "playbook-pi-hook-adapter.ts"
    if not source.is_file():
        raise ReconcileError(f"installed OMP bridge asset is missing: {source}")
    body = source.read_text(encoding="utf-8")
    provider_seam = 'const EMBEDDED_PROVIDER: "omp" | undefined = undefined;'
    if body.count(provider_seam) != 1:
        raise ReconcileError("OMP bridge provider seam is missing or ambiguous")
    specialized = body.replace(
        provider_seam, 'const EMBEDDED_PROVIDER: "omp" | undefined = "omp";'
    )
    legacy = "// playbook-managed: claude-playbook omp bridge schema=1\n" + specialized
    metadata = json.dumps(
        {
            "provider": "omp",
            "runtime_schema": RUNTIME_COMPAT_SCHEMA,
            "central_commit": runtime_commit(),
            "central_source": "git-checkout",
            "source": "scripts/playbook-pi-hook-adapter.ts",
            "root_launch_required": True,
        },
        indent=2,
        sort_keys=True,
    ) + "\n"
    guidance_intents, proposal_required = _guidance_intents(
        root, "AGENTS.md", guidance, "omp"
    )
    capability, detail = _guidance_capability(
        IntegrationCapability.FULL,
        "bare OMP enforcement at the project root",
        proposal_required,
    )
    return ProviderIntegration(
        "omp",
        Contribution(
            "omp",
            (
                *guidance_intents,
                ManagedFileIntent(
                    ".omp/extensions/playbook.ts",
                    specialized,
                    "scripts/playbook-pi-hook-adapter.ts",
                    marker_style="slash",
                    hook=True,
                    adopt_hashes=(_source_digest(specialized), _source_digest(legacy)),
                ),
                ManagedFileIntent(
                    ".omp/playbook.json",
                    metadata,
                    "tasks/provider_contributions:omp-metadata",
                    marker_style="json",
                    hook=True,
                ),
            ),
        ),
        capability,
        detail,
        ("OMP extension discovery is root-only; nested launches are not enforced",),
    )


def _antigravity_integration(root: Path, guidance: str) -> ProviderIntegration:
    advisory = (
        "<!-- playbook-harness: Antigravity 1.1.10 has no verified "
        "project-local hook loader; this guidance is advisory. -->\n\n"
        + guidance
    )
    guidance_intents, proposal_required = _guidance_intents(
        root, "GEMINI.md", advisory, "antigravity"
    )
    capability, detail = _guidance_capability(
        IntegrationCapability.GUIDANCE_ONLY,
        "Agy exposes only user-global plugin registration; init does not invoke it",
        proposal_required,
    )
    return ProviderIntegration(
        "antigravity",
        Contribution(
            "antigravity", guidance_intents
        ),
        capability,
        detail,
    )


def build_provider_integrations(
    root: Path,
    detections: tuple[ProviderDetection, ...],
    *,
    codex_feature_enabled: bool | None = None,
) -> tuple[ProviderIntegration, ...]:
    """Build deterministic local contributions for supported selected providers.

    Provider gates replace PENDING with their proven capability as their real
    hook payload is added. Keeping the status explicit prevents guidance files
    from being mistaken for enforcement during incremental development.
    """
    title = root.name.replace("-", " ").replace("_", " ").title()
    guidance = {
        "claude": ("CLAUDE.md", _standalone_guidance(claude_md(title))),
        "codex": ("AGENTS.md", _standalone_guidance(agents_md_template())),
        "antigravity": ("GEMINI.md", _standalone_guidance(agents_md_template())),
        "pi": ("AGENTS.md", _standalone_guidance(agents_md_template())),
        "omp": ("AGENTS.md", _standalone_guidance(agents_md_template())),
    }
    integrations = []
    supported = sorted(
        (item for item in detections if item.status == DetectionStatus.SUPPORTED),
        key=lambda item: _PROVIDER_RANK.get(item.name, len(_PROVIDER_RANK)),
    )
    for detection in supported:
        try:
            relative, content = guidance[detection.name]
        except KeyError as exc:
            raise ValueError(
                f"no provider contribution registered for {detection.name}"
            ) from exc
        if detection.name == "claude":
            integrations.append(_claude_integration(root, content))
            continue
        if detection.name == "codex":
            integrations.append(_codex_integration(root, content, codex_feature_enabled))
            continue
        if detection.name == "antigravity":
            integrations.append(_antigravity_integration(root, content))
            continue
        if detection.name == "pi":
            integrations.append(_pi_integration(root, content))
            continue
        if detection.name == "omp":
            integrations.append(_omp_integration(root, content))
            continue
        contribution = Contribution(
            detection.name, (CreateOnlyFileIntent(relative, content),)
        )
        integrations.append(
            ProviderIntegration(
                detection.name,
                contribution,
                IntegrationCapability.PENDING,
                "provider payload not yet contributed",
            )
        )
    return tuple(integrations)
