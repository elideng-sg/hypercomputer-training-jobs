#!/usr/bin/env python3
"""Guard against the kubespawner brace-expansion trap in singleuser profiles.

kubespawner passes every extra_annotations value through str.format() to expand
{username}/{userid}. A literal brace that is not doubled makes the spawn fail with
KeyError before the pod is ever created -- e.g. KeyError: '"interfaceName"' from the
networking.gke.io/interfaces JSON.

Run: python3 deploy/jupyter/test_profile_annotations.py
"""

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

# The identifiers kubespawner actually makes available to the template.
SPAWNER_NS = {
    "username": "testuser",
    "unescaped_username": "testuser",
    "legacy_escaped_username": "testuser",
    "userid": "1000",
    "servername": "",
    "unescaped_servername": "",
    "hubnamespace": "jupyter",
}


def check_file(path: Path) -> list[str]:
    values = yaml.safe_load(path.read_text()) or {}
    profiles = (values.get("singleuser") or {}).get("profileList") or []
    failures = []

    for profile in profiles:
        name = profile.get("display_name", "<unnamed>")
        annotations = (profile.get("kubespawner_override") or {}).get(
            "extra_annotations"
        ) or {}
        for key, value in annotations.items():
            if not isinstance(value, str):
                continue
            try:
                value.format(**SPAWNER_NS)
            except (KeyError, IndexError, ValueError) as exc:
                failures.append(
                    f"{path.name}: profile {name!r} annotation {key!r} is not "
                    f"format()-safe ({type(exc).__name__}: {exc}). "
                    f"Double every literal {{ and }}."
                )
    return failures


def main() -> int:
    here = Path(__file__).parent
    targets = sorted(here.glob("values*.yaml"))
    if not targets:
        print("no values*.yaml found", file=sys.stderr)
        return 1

    all_failures = []
    for path in targets:
        all_failures += check_file(path)

    if all_failures:
        print("FAIL — unescaped braces in profile annotations:\n")
        for failure in all_failures:
            print(f"  - {failure}")
        return 1

    print(f"PASS — annotations in {len(targets)} values file(s) are format()-safe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
