from ticketmaster.triage import DEFAULT_HOLDS, TRIAGE_DOC, hold_labels

DOC = """# Triage Labels

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `triage`             | Maintainer needs to evaluate this issue  |
| `needs-info`               | `waiting`            | Waiting on reporter for more information |
| `ready-for-agent`          | `afk`                | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `human only`         | Requires human implementation            |
"""


def write(root, text):
    path = root / TRIAGE_DOC
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_hold_labels_use_the_repos_names_for_needs_info_and_ready_for_human(tmp_path):
    write(tmp_path, DOC)
    holds = hold_labels(tmp_path)
    assert set(holds) == {"waiting", "human only"}
    assert holds["waiting"] == DEFAULT_HOLDS["needs-info"]
    assert holds["human only"] == DEFAULT_HOLDS["ready-for-human"]


def test_without_the_doc_the_canonical_names_hold(tmp_path):
    assert hold_labels(tmp_path) == DEFAULT_HOLDS
    assert hold_labels(None) == DEFAULT_HOLDS


def test_a_role_missing_from_the_doc_keeps_its_canonical_name(tmp_path):
    write(tmp_path, "| `needs-info` | `info?` | Waiting |\n")
    assert hold_labels(tmp_path) == {"info?": DEFAULT_HOLDS["needs-info"],
                                     "ready-for-human": DEFAULT_HOLDS["ready-for-human"]}
