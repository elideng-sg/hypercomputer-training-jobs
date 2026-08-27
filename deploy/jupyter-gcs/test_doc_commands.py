"""Guard the commands we hand to users in docs and example notebooks.

A user-facing command that cannot work is worse than no command: it sends people
looking for a fault in their own setup, and in this case it contradicts the very
isolation property the same documents advertise.

The case that motivated this file: every guide told users to run a bare

    gcloud storage ls          # "your bucket"

to find their workspace. That lists the *project's* buckets and needs
``storage.buckets.list``, which per-user KSAs deliberately do NOT have, so it
always 403s -- and elsewhere the same documents correctly cite that exact denial
as proof that users cannot enumerate each other's workspaces. Reported by a user
from a live notebook on 2026-08-27. It had also been copied into
``dataset_to_gcs.ipynb``, where ``MY_BUCKET`` was derived from it, so the worked
example failed at its first cell and every later cell inherited the failure.

The supported way to learn your own bucket name is to read it off your own mount,
which needs no IAM permission at all and stays correct even when a username has
to be sanitized:

    findmnt -n -o SOURCE ~/gcs      # -> hdlab-elideng-jupyter-<user>

    pytest deploy/jupyter-gcs/ -q
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# `gcloud storage ls` / `gsutil ls` with nothing after it but a comment.
BARE_LIST = re.compile(r"\b(?:gcloud storage|gsutil)\s+ls\s*(?:#.*)?$")

# An admin with project-level Storage permissions legitimately can list buckets;
# those invocations name the project explicitly, so they are not user-facing.
ADMIN_MARKERS = ("--project", "-p ")


def doc_lines():
    """(path, lineno, line) for every line of user-facing docs and examples."""
    paths = [
        p
        for pattern in ("docs/guides/*.md", "deploy/**/*.md", "deploy/**/*.ipynb")
        for p in REPO_ROOT.glob(pattern)
        if p.is_file()
    ]
    assert paths, "no docs found -- has the layout changed?"
    for path in sorted(paths):
        if path.suffix == ".ipynb":
            nb = json.loads(path.read_text())
            for i, cell in enumerate(nb.get("cells", [])):
                for line in "".join(cell.get("source", [])).splitlines():
                    yield path, f"cell {i}", line
        else:
            for n, line in enumerate(path.read_text().splitlines(), 1):
                yield path, n, line


def test_no_doc_tells_a_user_to_run_a_bare_gcloud_storage_ls():
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{where}: {line.strip()}"
        for path, where, line in doc_lines()
        if BARE_LIST.search(line.strip())
        and not any(m in line for m in ADMIN_MARKERS)
    ]
    assert not offenders, (
        "These lines tell a user to run a command that always fails with 403 "
        "(storage.buckets.list is deliberately denied to per-user KSAs). Use "
        "`findmnt -n -o SOURCE ~/gcs` to get the bucket name, then name the "
        "bucket explicitly:\n  " + "\n  ".join(offenders)
    )


def test_the_supported_bucket_discovery_command_is_documented():
    """The negative test above is satisfied by deleting every example, so pin the
    positive: the guide and the worked notebook must both teach `findmnt`."""
    must_teach = [
        REPO_ROOT / "docs/guides/04-jupyter-notebook-user-guide.md",
        REPO_ROOT / "deploy/jupyter/examples/dataset_to_gcs.ipynb",
    ]
    for path in must_teach:
        assert path.exists(), path
        assert "findmnt" in path.read_text(), (
            f"{path.relative_to(REPO_ROOT)} does not show users how to find their "
            "own bucket name; `findmnt -n -o SOURCE ~/gcs` is the supported way"
        )


def test_worked_notebook_defines_my_bucket_before_it_is_used():
    """Cells run top to bottom. MY_BUCKET used above its definition means the
    example breaks for anyone who runs it in order -- which is everyone."""
    nb = json.loads(
        (REPO_ROOT / "deploy/jupyter/examples/dataset_to_gcs.ipynb").read_text()
    )
    define_at = use_at = None
    for i, cell in enumerate(nb.get("cells", [])):
        if cell.get("cell_type") != "code":
            continue
        src = "".join(cell.get("source", []))
        if re.search(r"^\s*MY_BUCKET\s*=", src, re.M) and define_at is None:
            define_at = i
        if "MY_BUCKET" in src and use_at is None and define_at is None:
            use_at = i
    assert define_at is not None, "the notebook never defines MY_BUCKET"
    assert use_at is None, f"MY_BUCKET used in cell {use_at}, defined in {define_at}"


@pytest.mark.parametrize(
    "path",
    [
        REPO_ROOT / "docs/guides/04-jupyter-notebook-user-guide.md",
        REPO_ROOT / "deploy/jupyter-gcs/README.md",
    ],
    ids=lambda p: p.name,
)
def test_no_doc_promises_gcloud_auth_list_shows_the_users_own_email(path):
    """`gcloud auth list` prints the Workload Identity pool, not the person. A doc
    that implies otherwise makes correct output look broken."""
    text = path.read_text()
    if "gcloud auth list" not in text:
        pytest.skip("does not mention gcloud auth list")
    assert "svc.id.goog" in text, (
        f"{path.relative_to(REPO_ROOT)} shows `gcloud auth list` without saying it "
        "prints hdlab-elideng.svc.id.goog rather than the user's own address"
    )
