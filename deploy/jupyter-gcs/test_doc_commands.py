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

A second end-to-end run on 2026-08-27, after the fix above had been merged, found
three more things in the same documents that cannot work:

* Cell 13 of the worked notebook still called the bare list -- as
  ``subprocess.check_output(["gcloud", "storage", "ls"])``. The first version of
  this file matched only the shell spelling, anchored to end of line, so the suite
  stayed green over an unfixed 403. Hence ``BARE_LIST_ARGV``.
* ``huggingface-cli download`` prints "deprecated and no longer works" and
  downloads nothing under huggingface_hub 1.x, which this image ships. The
  replacement is ``hf download``. Hence ``DEAD_ENTRYPOINTS``.
* The dataset URL in cell 6 404s, and ``curl -fsSL`` fails silently, so the two
  read-back cells broke for a reason that looked unrelated. No test here: whether
  a URL resolves is a fact about the network, and this suite runs offline. The
  guard for that one is running the notebook, which is now part of the release
  check rather than something to assert.

The lesson each time is the same: a command in a document is only as good as the
last time somebody ran it on the live cluster.

    pytest deploy/jupyter-gcs/ -q
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# `gcloud storage ls` / `gsutil ls` with nothing after it but a comment.
# re.M so this also works when scanning a multi-line block, not just one line.
BARE_LIST = re.compile(r"\b(?:gcloud storage|gsutil)\s+ls\s*(?:#.*)?$", re.M)

# The same command spelled as an argv list, which is how Python calls it:
#     subprocess.check_output(["gcloud", "storage", "ls"])
# The first version of this file only had BARE_LIST, which is anchored to end of
# line and therefore matched nothing here -- so cell 13 of the worked notebook
# kept the 403 through a fix that was supposed to remove it, and the suite still
# went green. An argv list is a command too.
BARE_LIST_ARGV = re.compile(
    r"""\[\s*(?P<q>["'])"""
    r"""(?:gcloud(?P=q)\s*,\s*(?P=q)storage|gsutil)"""
    r"""(?P=q)\s*,\s*(?P=q)ls(?P=q)\s*\]"""
)

# Entry points that exist but do nothing. `huggingface-cli` was removed in
# huggingface_hub 1.x: it prints "deprecated and no longer works" and downloads
# nothing, so a user following the guide gets an empty directory. Prose may still
# name it (to warn people off), so only flag it outside backticks.
DEAD_ENTRYPOINTS = ("huggingface-cli",)
INLINE_CODE = re.compile(r"`[^`]*`")

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


def test_no_doc_calls_a_bare_gcloud_storage_ls_as_an_argv_list():
    """Same defect as above, spelled the way Python spells it. Kept separate so a
    failure names the shape that is wrong."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{where}: {line.strip()}"
        for path, where, line in doc_lines()
        if BARE_LIST_ARGV.search(line)
    ]
    assert not offenders, (
        "These lines call `gcloud storage ls` with no bucket as an argv list, "
        "which 403s exactly like the shell form. Get the name from the mount "
        "(`findmnt -n -o SOURCE ~/gcs`) and name the bucket:\n  "
        + "\n  ".join(offenders)
    )


def test_no_doc_invokes_an_entrypoint_that_no_longer_works():
    """`huggingface-cli download ...` exits non-zero having downloaded nothing.
    Mentions inside backticks are fine -- the guides name it to warn people off."""
    offenders = []
    for path, where, line in doc_lines():
        bare = INLINE_CODE.sub("", line)
        for dead in DEAD_ENTRYPOINTS:
            if dead in bare:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{where}: {line.strip()}"
                )
    assert not offenders, (
        "`huggingface-cli` was removed in huggingface_hub 1.x -- it prints a "
        "deprecation notice and downloads nothing. Use `hf` instead:\n  "
        + "\n  ".join(offenders)
    )


def test_the_hand_authored_google_doc_source_stays_in_step():
    """docs/export/jupyter-gcs-workspace-user-guide.html has no Markdown source --
    it is written by hand and uploaded to the internal Doc -- so the checks above
    cannot reach it. Guard the two commands that were wrong in it."""
    doc = REPO_ROOT / "docs/export/jupyter-gcs-workspace-user-guide.html"
    assert doc.exists(), doc
    text = doc.read_text()

    # Only <pre> blocks: the surrounding prose deliberately names the broken
    # commands in order to warn people off them, so a plain substring search over
    # the whole file flags the warnings themselves. The HTML comment header goes
    # too -- it is maintainer notes about the Docs importer, not document content,
    # and it quotes both broken commands while explaining them.
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    blocks = re.findall(r"<pre[^>]*>(.*?)</pre>", body, re.S)
    assert blocks, "no <pre> blocks -- has the Doc source been restructured?"

    # Transcript blocks that show a command together with the error it produces
    # are counter-examples: §3 and §7 quote the bare `gcloud storage ls` 403 on
    # purpose, to demonstrate that the isolation holds. Those are the point, not
    # a defect, so judge only the blocks that read as instructions.
    commands = "\n".join(
        b for b in blocks if "ERROR" not in b and "DENIED" not in b
    )

    for bad in ("huggingface-cli", '"gcloud", "storage", "ls"'):
        assert bad not in commands, (
            f"{doc.relative_to(REPO_ROOT)} runs `{bad}` in a code block, which does "
            "not work. Fix it here AND re-upload the Doc -- this file is the Doc's "
            "source, so the two drift apart silently."
        )
    bare = BARE_LIST.search(commands)
    assert not bare, (
        f"{doc.relative_to(REPO_ROOT)} runs a bare `{bare.group(0) if bare else ''}` "
        "in a code block; it 403s. Use `findmnt -n -o SOURCE ~/gcs`."
    )
    assert "hf download" in commands, "the Doc source no longer shows how to download"


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


# `gcloud storage rsync [-flags] <source>`, capturing the source only when it is a
# LOCAL path (`~/...`, `/...`, `./...`), quoted or not. A `gs://` source is someone
# else's data being pulled in, which says nothing about whether this cell wrote
# anything.
#
# The optional quote matters: without it `rsync -r "~/scratch/ckpt"` does not match
# and the cell is skipped silently -- the same kind of hole that let an unfixed 403
# sit behind a green suite in #22. A source spelled with a variable (`$HOME/...`)
# still escapes this; write paths literally in the notebook.
RSYNC_SOURCE = re.compile(
    r"""gcloud storage rsync\s+(?:-\S+\s+)*["']?"""
    r"""(?P<src>(?:~|\.{0,2}/)[\w./-]*)(?=["'\s])"""
)


def test_worked_notebook_never_syncs_a_directory_it_left_empty():
    """A cell that rsyncs a directory nothing has written to prints
    ``Completed files 0 | 0B`` and demonstrates nothing.

    Found by running the notebook on the cluster on 2026-08-27: the checkpoint
    cell did ``mkdir -p ~/scratch/ckpt``, left a comment where a training loop's
    write would go, and then synced it -- so the bucket had no ``checkpoints/``
    prefix at all afterwards, and a user could not tell the pattern from a
    no-op. A cell that runs has to do the thing it is teaching.
    """
    nb = json.loads(
        (REPO_ROOT / "deploy/jupyter/examples/dataset_to_gcs.ipynb").read_text()
    )
    for i, cell in enumerate(nb.get("cells", [])):
        if cell.get("cell_type") != "code":
            continue
        src = "".join(cell.get("source", []))
        # Commented-out lines are illustrations, not things the cell runs. Cell 10
        # carries a `# !gcloud storage rsync -r gs://...` example, and judging it
        # would be judging prose.
        live = "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )
        match = RSYNC_SOURCE.search(live)
        if not match:
            continue
        source_dir = match.group("src")
        # Lines that touch the directory for a reason other than creating it or
        # syncing it -- i.e. something that puts a file in it.
        writers = [
            line
            for line in src.splitlines()
            if source_dir in line
            and not line.lstrip().startswith("#")
            and "mkdir" not in line
            and "rsync" not in line
        ]
        assert writers, (
            f"cell {i} syncs {source_dir}, but nothing in that cell writes a file "
            "there, so it copies zero bytes and proves nothing. Have the cell "
            "create a stand-in file before the sync."
        )


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
