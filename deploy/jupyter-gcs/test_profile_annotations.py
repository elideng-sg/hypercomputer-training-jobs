"""Guard against the kubespawner brace-expansion trap in singleuser profiles.

kubespawner passes every ``extra_annotations`` value through ``str.format()`` in
``_expand_user_properties()`` so ``{username}``/``{userid}`` expand. A literal
brace that is not doubled makes the spawn fail with a ``KeyError`` before the pod
is ever created -- in production this was ``KeyError: '"interfaceName"'`` from the
``networking.gke.io/interfaces`` JSON, and it killed every GPU spawn.

Originally written on the `fix/jupyter-tcpxo-spawn` branch, which never merged;
carried here and extended to also assert the *rendered* annotation is the JSON
GKE expects, not merely that formatting did not raise.

    pytest deploy/jupyter-gcs/ -q
"""

import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML needed to parse the values files")

REPO_ROOT = Path(__file__).resolve().parents[2]

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

TCPXO_VALUES = REPO_ROOT / "deploy/tcpxo-migration/03-jupyter-values-tcpxo.yaml"


def values_files():
    """Every Helm values file in the repo that could carry a profileList."""
    found = sorted(
        p
        for p in REPO_ROOT.glob("deploy/**/*.yaml")
        if "values" in p.name and p.is_file()
    )
    assert found, "no values files found -- has the layout changed?"
    return found


def annotations_in(path):
    values = yaml.safe_load(path.read_text()) or {}
    profiles = (values.get("singleuser") or {}).get("profileList") or []
    for profile in profiles:
        name = profile.get("display_name", "<unnamed>")
        override = profile.get("kubespawner_override") or {}
        for key, value in (override.get("extra_annotations") or {}).items():
            if isinstance(value, str):
                yield name, key, value


@pytest.mark.parametrize("path", values_files(), ids=lambda p: p.name)
def test_profile_volume_overrides_are_mappings_not_lists(path):
    """A profile that overrides `volumes` as a LIST silently deletes the home PVC.

    kubespawner's _apply_overrides merges an override into the existing trait only
    when both are dicts (`recursive_update`); anything else is a plain setattr.
    z2jh sets c.KubeSpawner.volumes to a dict keyed by volume name, so a list
    override replaces the whole mapping -- home directory included. The notebook
    still starts, and the user's files vanish when the pod is replaced.

    Observed live on 2026-08-27: an 8-GPU spawn had only nvidia/aperture/shm
    mounted and no claim-<user>.
    """
    values = yaml.safe_load(path.read_text()) or {}
    for profile in (values.get("singleuser") or {}).get("profileList") or []:
        override = profile.get("kubespawner_override") or {}
        for key in ("volumes", "volume_mounts"):
            if key in override:
                assert isinstance(override[key], dict), (
                    f"{path.name}: profile {profile.get('display_name')!r} sets "
                    f"{key} as a {type(override[key]).__name__}; it must be a "
                    "mapping keyed by volume name so it merges with the chart's "
                    "home volume instead of replacing it"
                )


@pytest.mark.parametrize("path", values_files(), ids=lambda p: p.name)
def test_every_profile_annotation_survives_str_format(path):
    for profile, key, value in annotations_in(path):
        try:
            value.format(**SPAWNER_NS)
        except (KeyError, IndexError, ValueError) as exc:
            pytest.fail(
                f"{path.relative_to(REPO_ROOT)}: profile {profile!r} annotation "
                f"{key!r} is not format()-safe ({type(exc).__name__}: {exc}). "
                "Double every literal { and }."
            )


def test_tcpxo_interfaces_render_to_the_json_gke_expects():
    """The negative test above passes for a value with no braces at all, so pin
    the actual contract: nine interfaces, eth0 on the default network."""
    matches = [
        value
        for _, key, value in annotations_in(TCPXO_VALUES)
        if key == "networking.gke.io/interfaces"
    ]
    assert len(matches) == 1, "expected exactly one TCPXO interfaces annotation"

    rendered = matches[0].format(**SPAWNER_NS)
    interfaces = json.loads(rendered)
    assert len(interfaces) == 9
    assert interfaces[0] == {"interfaceName": "eth0", "network": "default"}
    assert [i["interfaceName"] for i in interfaces] == [f"eth{n}" for n in range(9)]
    assert [i["network"] for i in interfaces[1:]] == [f"gpu-net-{n}" for n in range(8)]


def test_source_file_stores_the_braces_doubled():
    """Documents *why* the source looks odd, so a future cleanup pass that
    un-doubles the braces trips here as well as in the format() test."""
    text = TCPXO_VALUES.read_text()
    assert '{{"interfaceName":"eth0","network":"default"}}' in text
    assert '\n            {"interfaceName"' not in text
