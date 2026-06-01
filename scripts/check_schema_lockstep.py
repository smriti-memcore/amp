#!/usr/bin/env python3
"""Schema lockstep checker — fails CI when amp.json and amp-openapi.yaml drift.

The AMP repo has two schema sources of truth:
- schema/amp.json         — canonical for the MCP / JSON-RPC channel
- schema/amp-openapi.yaml — canonical for the Standalone REST channel

Spec rule (Appendix C, §3.5): they MUST describe the same wire shape for every
field that appears in both. Drift between the two has historically been the
single most common review finding — see PR #11, PR #14, PR #16 reviews where
Codex hand-spotted maxItems / additionalProperties / minItems / required-set
disagreements that would have failed automatically with this script.

This script is intentionally narrow: it only enforces invariants that the spec
calls out as MUSTs across both channels. Channel-specific extensions are
allowed (e.g. OpenAPI carries HTTP-layer concepts like 4xx response bodies
that have no JSON-RPC analogue, and amp.json carries tool annotations that
have no REST analogue). The script lists every divergence it finds and exits
non-zero if any are CRITICAL.

Usage:
    python scripts/check_schema_lockstep.py
    python scripts/check_schema_lockstep.py --strict   # also fail on WARNINGs

Exit codes:
    0 — no critical divergences
    1 — at least one critical divergence
    2 — script error (file missing, parse failure, etc.)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    print("ERROR: PyYAML required (`pip install pyyaml`)", file=sys.stderr)
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parent.parent
AMP_JSON_PATH = REPO_ROOT / "schema" / "amp.json"
OPENAPI_PATH = REPO_ROOT / "schema" / "amp-openapi.yaml"


# Map from amp.json verb-input/output to OpenAPI component schema name.
# Only shapes that appear in BOTH channels are listed here; channel-specific
# shapes (e.g. ToolAnnotations, AmpErrorData) are excluded by design.
SHARED_SHAPES: list[tuple[str, str, str]] = [
    # (amp.json path, OpenAPI schema name, human-readable label)
    # Shared definitions used by multiple verbs:
    ("definitions/Scope",                       "Scope",                  "Scope"),
    ("definitions/MemoryResult",                "MemoryResult",           "MemoryResult"),
    ("definitions/RecallFilters",               "RecallFilters",          "RecallFilters"),
    ("definitions/MetadataFilter",              "MetadataFilter",         "MetadataFilter (v1.2-draft)"),
    # Per-verb input/output shapes live under tools/<verb>:
    ("tools/amp.encode/input",                  "EncodeRequest",          "amp.encode input"),
    ("tools/amp.encode/output",                 "EncodeResponse",         "amp.encode output"),
    ("tools/amp.recall/input",                  "RecallRequest",          "amp.recall input"),
    ("tools/amp.recall/output",                 "RecallResponse",         "amp.recall output"),
    ("tools/amp.forget/output",                 "ForgetResponse",         "amp.forget output"),
    ("tools/amp.pin/output",                    "PinResponse",            "amp.pin output"),
    ("tools/amp.consolidate/input",             "ConsolidateRequest",     "amp.consolidate input"),
    ("tools/amp.consolidate/output",            "ConsolidateResponse",    "amp.consolidate output"),
    ("tools/amp.update/input",                  "UpdateRequest",          "amp.update input (v1.2-draft)"),
    ("tools/amp.update/output",                 "UpdateResponse",         "amp.update output (v1.2-draft)"),
    ("tools/amp.batch_encode/input",            "BatchEncodeRequest",     "amp.batch_encode input (v1.2-draft)"),
    ("tools/amp.batch_encode/output",           "BatchEncodeResponse",    "amp.batch_encode output (v1.2-draft)"),
    ("tools/amp.export/input",                  "ExportRequest",          "amp.export input"),
    ("tools/amp.import/input",                  "ImportRequest",          "amp.import input"),
    ("tools/amp.import/output",                 "ImportResponse",         "amp.import output"),
]


# Properties that legitimately differ between channels — the comparator ignores
# them when computing required-set / property-presence findings.
#
# Rationale: REST encodes some fields as URL path parameters (PATCH /v1/memories/{id})
# rather than body fields, while the MCP/JSON-RPC channel always carries them
# in the JSON request body. The schemas correctly model this asymmetry.
CHANNEL_SPECIFIC_PROPERTIES: dict[str, set[str]] = {
    # OpenAPI carries `id` on PATCH /v1/memories/{id} (path param) and DELETE
    # /v1/memories/{id}; the JSON-RPC tool input has `id` as a body field.
    "amp.update input (v1.2-draft)": {"id"},
    "amp.forget input": {"id"},
    "amp.pin input": {"id"},
}


class Finding:
    """A single divergence between the two schemas."""

    def __init__(self, severity: str, shape: str, detail: str) -> None:
        self.severity = severity  # CRITICAL | WARNING | INFO
        self.shape = shape
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.severity}: [{self.shape}] {self.detail}"


def _load_amp_json() -> dict[str, Any]:
    try:
        return json.loads(AMP_JSON_PATH.read_text())
    except FileNotFoundError:
        print(f"ERROR: {AMP_JSON_PATH} not found", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as e:
        print(f"ERROR: {AMP_JSON_PATH} is not valid JSON: {e}", file=sys.stderr)
        sys.exit(2)


def _load_openapi() -> dict[str, Any]:
    try:
        return yaml.safe_load(OPENAPI_PATH.read_text())
    except FileNotFoundError:
        print(f"ERROR: {OPENAPI_PATH} not found", file=sys.stderr)
        sys.exit(2)
    except yaml.YAMLError as e:
        print(f"ERROR: {OPENAPI_PATH} is not valid YAML: {e}", file=sys.stderr)
        sys.exit(2)


def _walk(d: dict[str, Any], path: str) -> Any:
    """Resolve a path like 'definitions/amp.encode/input' against a dict."""
    cur: Any = d
    for part in path.split("/"):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _required_set(node: dict[str, Any]) -> set[str]:
    req = node.get("required") or []
    return set(req) if isinstance(req, list) else set()


def _properties(node: dict[str, Any]) -> dict[str, Any]:
    return node.get("properties") or {}


def _enum(node: dict[str, Any]) -> list[Any] | None:
    return node.get("enum")


def _max_items(node: dict[str, Any]) -> int | None:
    v = node.get("maxItems")
    return v if isinstance(v, int) else None


def _min_items(node: dict[str, Any]) -> int | None:
    v = node.get("minItems")
    return v if isinstance(v, int) else None


def _additional_properties(node: dict[str, Any]) -> Any:
    # Returns the actual value (True / False / dict / None-if-omitted).
    if "additionalProperties" not in node:
        return "OMITTED"
    return node["additionalProperties"]


def compare_required_sets(label: str, amp_node: dict[str, Any], openapi_node: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    ignored = CHANNEL_SPECIFIC_PROPERTIES.get(label, set())
    a = _required_set(amp_node) - ignored
    b = _required_set(openapi_node) - ignored
    # Properties that are required in one channel but not the other are
    # critical drift — clients generated from one will not validate against
    # the other.
    only_amp = a - b
    only_oapi = b - a
    if only_amp:
        findings.append(Finding(
            "CRITICAL", label,
            f"required in amp.json but NOT in OpenAPI: {sorted(only_amp)}",
        ))
    if only_oapi:
        findings.append(Finding(
            "CRITICAL", label,
            f"required in OpenAPI but NOT in amp.json: {sorted(only_oapi)}",
        ))
    return findings


def compare_additional_properties(label: str, amp_node: dict[str, Any], openapi_node: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    a, b = _additional_properties(amp_node), _additional_properties(openapi_node)
    # Strict-set mismatch (false vs anything-else) is critical: it changes
    # whether unknown keys pass validation.
    a_strict = a is False
    b_strict = b is False
    if a_strict != b_strict:
        findings.append(Finding(
            "CRITICAL", label,
            f"additionalProperties disagreement — amp.json={a!r}, OpenAPI={b!r} "
            f"(strict-set means unknown keys are rejected; mismatch lets typo "
            f"fields slip through on one channel)",
        ))
    return findings


def compare_property_enums(label: str, amp_node: dict[str, Any], openapi_node: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    amp_props, oapi_props = _properties(amp_node), _properties(openapi_node)
    shared = set(amp_props.keys()) & set(oapi_props.keys())
    for prop in sorted(shared):
        a_enum = _enum(amp_props[prop])
        b_enum = _enum(oapi_props[prop])
        if a_enum is None and b_enum is None:
            continue
        if a_enum is None or b_enum is None:
            findings.append(Finding(
                "WARNING", label,
                f"property '{prop}' has enum in one channel but not the other "
                f"(amp.json={a_enum}, OpenAPI={b_enum})",
            ))
            continue
        if set(a_enum) != set(b_enum):
            findings.append(Finding(
                "CRITICAL", label,
                f"property '{prop}' enum disagreement — "
                f"amp.json={sorted(a_enum)}, OpenAPI={sorted(b_enum)}",
            ))
    return findings


def compare_array_bounds(label: str, amp_node: dict[str, Any], openapi_node: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    amp_props, oapi_props = _properties(amp_node), _properties(openapi_node)
    shared = set(amp_props.keys()) & set(oapi_props.keys())
    for prop in sorted(shared):
        a_max = _max_items(amp_props[prop])
        b_max = _max_items(oapi_props[prop])
        a_min = _min_items(amp_props[prop])
        b_min = _min_items(oapi_props[prop])
        if a_max != b_max:
            findings.append(Finding(
                "CRITICAL", label,
                f"property '{prop}' maxItems disagreement — "
                f"amp.json={a_max}, OpenAPI={b_max}",
            ))
        if a_min != b_min:
            findings.append(Finding(
                "CRITICAL", label,
                f"property '{prop}' minItems disagreement — "
                f"amp.json={a_min}, OpenAPI={b_min}",
            ))
    return findings


def compare_property_presence(label: str, amp_node: dict[str, Any], openapi_node: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    ignored = CHANNEL_SPECIFIC_PROPERTIES.get(label, set())
    a_props = set(_properties(amp_node).keys()) - ignored
    b_props = set(_properties(openapi_node).keys()) - ignored
    only_amp = a_props - b_props
    only_oapi = b_props - a_props
    # Channel-specific extensions are allowed but worth surfacing as INFO so
    # an extra field doesn't go unreviewed.
    if only_amp:
        findings.append(Finding(
            "WARNING", label,
            f"properties only in amp.json (channel extension or drift): "
            f"{sorted(only_amp)}",
        ))
    if only_oapi:
        findings.append(Finding(
            "WARNING", label,
            f"properties only in OpenAPI (channel extension or drift): "
            f"{sorted(only_oapi)}",
        ))
    return findings


def check_shape(
    amp_root: dict[str, Any],
    oapi_schemas: dict[str, Any],
    amp_path: str,
    openapi_name: str,
    label: str,
) -> list[Finding]:
    findings: list[Finding] = []
    amp_node = _walk(amp_root, amp_path)
    oapi_node = oapi_schemas.get(openapi_name)

    if amp_node is None and oapi_node is None:
        # Neither channel defines this shape — fine, drop it from coverage.
        return findings
    if amp_node is None:
        findings.append(Finding(
            "WARNING", label,
            f"defined in OpenAPI ({openapi_name}) but missing from amp.json "
            f"({amp_path}) — JSON-RPC clients have no schema to validate against",
        ))
        return findings
    if oapi_node is None:
        findings.append(Finding(
            "WARNING", label,
            f"defined in amp.json ({amp_path}) but missing from OpenAPI "
            f"({openapi_name}) — REST clients have no schema to generate from",
        ))
        return findings

    findings.extend(compare_required_sets(label, amp_node, oapi_node))
    findings.extend(compare_additional_properties(label, amp_node, oapi_node))
    findings.extend(compare_property_enums(label, amp_node, oapi_node))
    findings.extend(compare_array_bounds(label, amp_node, oapi_node))
    findings.extend(compare_property_presence(label, amp_node, oapi_node))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on WARNINGs too, not just CRITICALs.",
    )
    args = parser.parse_args()

    amp_root = _load_amp_json()
    openapi = _load_openapi()
    oapi_schemas = openapi.get("components", {}).get("schemas", {})

    if not oapi_schemas:
        print("ERROR: OpenAPI document has no components.schemas section", file=sys.stderr)
        return 2

    all_findings: list[Finding] = []
    print("schema lockstep check — amp.json ↔ amp-openapi.yaml")
    print("=" * 70)
    for amp_path, oapi_name, label in SHARED_SHAPES:
        shape_findings = check_shape(amp_root, oapi_schemas, amp_path, oapi_name, label)
        all_findings.extend(shape_findings)
        if shape_findings:
            print(f"\n[{label}]  ({len(shape_findings)} finding{'s' if len(shape_findings) != 1 else ''})")
            for f in shape_findings:
                print(f"  {f.severity}: {f.detail}")

    print()
    print("=" * 70)
    by_sev = {"CRITICAL": 0, "WARNING": 0, "INFO": 0}
    for f in all_findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
    crit = by_sev.get("CRITICAL", 0)
    warn = by_sev.get("WARNING", 0)
    info = by_sev.get("INFO", 0)
    print(f"Summary: {crit} CRITICAL, {warn} WARNING, {info} INFO across "
          f"{len(SHARED_SHAPES)} shared shapes")

    if crit > 0:
        print("\n❌ FAIL — schema lockstep violated.")
        return 1
    if args.strict and warn > 0:
        print("\n❌ FAIL — --strict in effect and WARNINGs present.")
        return 1
    print("\n✅ PASS — schemas are in lockstep.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
