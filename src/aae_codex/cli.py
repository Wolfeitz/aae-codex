from __future__ import annotations

import argparse
from datetime import date
import hashlib
import importlib.resources
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Iterable, cast

from aae.hooks import find_aae_root, process_event


MAX_PAYLOAD_BYTES = 1_048_576
MAX_CONTEXT_CHARS = 8_000
EDIT_TOOLS = {"Edit", "Write", "apply_patch"}
PATCH_PATH = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File: (?P<path>.+)$", re.MULTILINE
)
PROJECTION_MARKER = "<!-- aae-adapter-projection: codex"


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _portable_path(root: Path, value: str) -> str | None:
    if not value or len(value) > 4096 or any(char in value for char in "\n\r\x00"):
        return None
    candidate = Path(value.replace("\\", "/"))
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(root.resolve())
        except ValueError:
            return None
    portable = candidate.as_posix().removeprefix("./")
    return None if not portable or ".." in Path(portable).parts else portable


def _changed_paths(root: Path, tool_input: object) -> list[str]:
    if not isinstance(tool_input, dict):
        return []
    values: list[str] = []
    for key in ("file_path", "filePath", "path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            values.append(value)
    patch = tool_input.get("patch", tool_input.get("command"))
    if isinstance(patch, str):
        values.extend(match.group("path").strip() for match in PATCH_PATH.finditer(patch))
    return sorted({path for value in values if (path := _portable_path(root, value))})


def _template_root() -> Any:
    return importlib.resources.files("aae_codex").joinpath("templates")


def install(root: Path) -> int:
    installed: list[str] = []
    preserved: list[str] = []
    for resource in _template_root().rglob("*"):
        if not resource.is_file() or resource.name.endswith(".pyc"):
            continue
        relative = Path(*resource.relative_to(_template_root()).parts)
        destination = root / relative
        if destination.exists():
            preserved.append(relative.as_posix())
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with importlib.resources.as_file(resource) as source:
            shutil.copyfile(source, destination)
        installed.append(relative.as_posix())
    sync_errors = sync_skills(root)
    print(json.dumps({"installed": installed, "preserved": preserved, "skill_errors": sync_errors}, indent=2))
    return 1 if sync_errors else 0


def _projected_skill(manifest: dict[str, Any], procedure: str, source_sha256: str) -> str:
    name = str(manifest["name"])
    description = str(manifest["description"]).replace("\n", " ").strip()
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {json.dumps(description)}\n"
        "---\n"
        f"{PROJECTION_MARKER} source-sha256: {source_sha256} -->\n\n"
        + procedure.lstrip()
    )


def sync_skills(root: Path) -> list[str]:
    errors: list[str] = []
    source_root = root / ".aae/skills"
    if not source_root.is_dir():
        return []
    for manifest_path in sorted(source_root.glob("*/skill.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            procedure_path = manifest_path.parent / str(manifest.get("procedure", "SKILL.md"))
            procedure = procedure_path.read_text(encoding="utf-8")
            name = manifest["name"]
            if not isinstance(name, str) or not isinstance(manifest.get("description"), str):
                raise ValueError("name and description must be strings")
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError) as error:
            errors.append(f"{manifest_path}: {error}")
            continue
        source_sha256 = hashlib.sha256(
            (manifest_path.read_bytes() + b"\0" + procedure.encode())
        ).hexdigest()
        destination = root / ".agents/skills" / name / "SKILL.md"
        if destination.exists() and PROJECTION_MARKER not in destination.read_text(encoding="utf-8"):
            errors.append(f"Preserved non-AAE native skill: {destination}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            _projected_skill(cast(dict[str, Any], manifest), procedure, source_sha256),
            encoding="utf-8",
        )
    return errors


def handle_hook(start: Path, native: dict[str, Any]) -> dict[str, Any] | None:
    root = find_aae_root(start)
    if root is None:
        return None
    event_name = native.get("hook_event_name")
    tool_name = native.get("tool_name")
    if event_name != "PostToolUse" or tool_name not in EDIT_TOOLS:
        return None
    paths = _changed_paths(root, native.get("tool_input", {}))
    if not paths:
        return None
    native_sha256 = _digest(native)
    identifiers = {
        key: native[key]
        for key in ("session_id", "turn_id", "tool_use_id")
        if isinstance(native.get(key), str)
    }
    record, procedures, errors = process_event(
        root,
        event="files-changed",
        payload={"paths": paths},
        idempotency_key="codex:" + _digest({"payload": native_sha256, **identifiers}),
        record_no_match=False,
        delivery_provenance={
            "adapter": "codex",
            "native_event": event_name,
            "payload_sha256": native_sha256,
            "tool_name": tool_name,
            "paths": paths,
            **identifiers,
        },
    )
    messages = list(procedures.values())
    if errors:
        messages.append("AAE adapter errors: " + "; ".join(errors))
    failed = record.get("status") in {
        "failed",
        "denied",
        "configuration-invalid",
        "chain-depth-denied",
        "action-budget-denied",
    }
    if failed and not errors:
        messages.append(f"AAE event status: {record.get('status')}")
    if not messages:
        return None
    context = "\n\n".join(messages)[:MAX_CONTEXT_CHARS]
    output: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }
    if failed:
        output.update({"decision": "block", "reason": context})
    return output


def hook_command(path: Path) -> int:
    raw = sys.stdin.read(MAX_PAYLOAD_BYTES + 1)
    if len(raw.encode()) > MAX_PAYLOAD_BYTES:
        print("Codex hook payload exceeds 1 MiB", file=sys.stderr)
        return 1
    try:
        native = json.loads(raw)
    except json.JSONDecodeError as error:
        print(f"Invalid Codex hook JSON: {error}", file=sys.stderr)
        return 1
    if not isinstance(native, dict):
        print("Codex hook payload must be an object", file=sys.stderr)
        return 1
    output = handle_hook(path, native)
    if output is not None:
        print(json.dumps(output, separators=(",", ":")))
    return 0


def _capabilities() -> dict[str, Any]:
    resource = importlib.resources.files("aae_codex").joinpath("capabilities.json")
    return cast(dict[str, Any], json.loads(resource.read_text(encoding="utf-8")))


def doctor(root: Path, strict: bool) -> int:
    manifest = _capabilities()
    executable = shutil.which("codex")
    version = None
    hooks_feature = False
    if executable:
        version_result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, check=False
        )
        version = version_result.stdout.strip() or version_result.stderr.strip()
        feature_result = subprocess.run(
            [executable, "features", "list"], capture_output=True, text=True, check=False
        )
        hooks_feature = any(
            line.split()[:1] == ["hooks"] and line.split()[-1:] == ["true"]
            for line in feature_result.stdout.splitlines()
        )
    tested = manifest["verified_versions"]["cli"]
    verified = bool(version and any(item in version for item in tested))
    age_days = (date.today() - date.fromisoformat(manifest["verified_at"])).days
    project_files = {
        ".codex/hooks.json": (root / ".codex/hooks.json").is_file(),
        ".codex/agents/aae-independent-reviewer.toml": (
            root / ".codex/agents/aae-independent-reviewer.toml"
        ).is_file(),
    }
    result = {
        "adapter": "codex",
        "runtime": {"executable": executable, "version": version},
        "verified_version": verified,
        "verification_age_days": age_days,
        "native_hooks_available": hooks_feature,
        "project_files": project_files,
    }
    print(json.dumps(result, indent=2))
    healthy = bool(executable and verified and hooks_feature and all(project_files.values()))
    return 1 if strict and not healthy else 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="AAE native Codex adapter")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("init", "sync-skills"):
        command = commands.add_parser(name)
        command.add_argument("path", nargs="?", default=".")
    hook = commands.add_parser("hook")
    hook.add_argument("path", nargs="?", default=".")
    doctor_parser = commands.add_parser("doctor")
    doctor_parser.add_argument("path", nargs="?", default=".")
    doctor_parser.add_argument("--strict", action="store_true")
    return root


def main(argv: Iterable[str] | None = None) -> int:
    arguments = parser().parse_args(list(argv) if argv is not None else None)
    root = Path(arguments.path).resolve()
    if arguments.command == "init":
        return install(root)
    if arguments.command == "sync-skills":
        errors = sync_skills(root)
        print(json.dumps({"errors": errors}, indent=2))
        return 1 if errors else 0
    if arguments.command == "hook":
        return hook_command(root)
    return doctor(root, arguments.strict)
