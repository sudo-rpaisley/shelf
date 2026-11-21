#!/usr/bin/env python3
"""Audit generated-artifact differences between two Git revisions.

This retained upstream comparison utility is NOT this fork's pull-request policy
gate. Authors regenerate and commit CSS, the service-worker digest and test-count
badges before submission, including in pull requests. Read-only CI checks their
freshness against source; it never pushes an automatic restamp commit.

    python scripts/check_pr_generated.py                  # HEAD^1 vs HEAD
    python scripts/check_pr_generated.py --base X --head Y

Exit0 means identical parsed artifact values, not a successful build. Exit1 means
differences or an invalid/unparseable revision; differences require review, not
reverting correctly regenerated output. Defaults compare HEAD with its first
parent, so normal author-generated changes are expected to produce exit1.

Not a make target or CI job. Use make css/check-sw-version/check-badges and the
read-only CSS rebuild comparison to decide whether submitted output is current.
"""

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent


def _load(name):
    """Load a sibling script standalone, the way tests/ loads these two.

    `scripts/` is not a package, so an ordinary import works only when the
    script is run directly. Loading by path works everywhere and is the same
    mechanism `tests/test_badge_stamp.py` and `tests/test_store.py` already
    use, which is what keeps this file honest: it consults the *real* parsers
    rather than a second copy of their regexes that could drift out of step.
    """
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stamp_sw_version = _load("stamp_sw_version")
stamp_test_badges = _load("stamp_test_badges")

CSS_PATH = "static/css/app.css"
SW_PATH = "static/sw.js"
README_PATH = "README.md"

# Every path this check reads out of both revisions.
TRACKED_PATHS = (CSS_PATH, SW_PATH, README_PATH)


class GitError(Exception):
    """A revision could not be resolved — the caller passed something wrong."""


def read_blobs(rev, paths=TRACKED_PATHS):
    """Return {path: bytes or None} for one revision. None means absent.

    Paths are read as `<rev>:./<path>`. The `./` form resolves against the
    working directory, so the same string works from the app directory of the
    monorepo (where the files live under `shelf/`) and from the root of the
    public repo (where they do not). Nothing here needs to know which it is in.
    """
    resolved = resolve_rev(rev)
    blobs = {}
    for path in paths:
        proc = subprocess.run(
            ["git", "show", f"{resolved}:./{path}"],
            capture_output=True,
        )
        # A path missing from a revision is a fact about the tree, not an
        # error: it is reported as a difference if the other side has it.
        blobs[path] = proc.stdout if proc.returncode == 0 else None
    return blobs


def resolve_rev(rev):
    """Resolve a revision to a concrete commit SHA, or raise GitError.

    Resolving up front is what lets `read_blobs` treat a failed `git show` as
    "the path is absent" rather than "the revision was bad" — and it is what
    makes the printed restore command name a SHA a contributor can actually
    use, instead of echoing back a relative ref like `HEAD^1` that means
    something different in their clone than it does in the CI checkout.
    """
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", f"{rev}^{{commit}}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise GitError(
            f"Could not resolve the revision {rev!r}: "
            f"{proc.stderr.strip() or 'git rev-parse failed'}.\n"
            "  On a pull_request build this usually means the checkout is too "
            "shallow — actions/checkout needs `fetch-depth: 2` for HEAD^1 to "
            "exist."
        )
    return proc.stdout.strip()


class Difference:
    """One generated artefact that does not match the base."""

    def __init__(self, artefact, base_value=None, whole_file=False):
        self.artefact = artefact
        self.base_value = base_value
        self.whole_file = whole_file

    def describe(self, base_sha):
        """Describe the difference without instructing an artifact rollback."""
        if self.whole_file:
            return (
                f"  {self.artefact} differs from the base.\n"
                f"      Review the rebuild against its source; base revision: {base_sha}."
            )
        return (
            f"  {self.artefact} differs from the base.\n"
            f"      Base value: {self.base_value}; verify the new value against source.\n"
            f"      Unrelated edits in this file are outside this audit."
        )


def _badge_counts(src):
    """{badge label: count} from one README's bytes, via the stamp script's own regex."""
    text = src.decode("utf-8")
    counts = {}
    for slug, _args in stamp_test_badges.SUITES:
        match = stamp_test_badges._badge_re(slug).search(text)
        if not match:
            raise stamp_test_badges.BadgeParseError(
                f"No `{slug}-<n>%20passing` badge found in {README_PATH} — either "
                "the badge was renamed or scripts/stamp_test_badges.py's regex no "
                "longer matches it. Refusing to compare a file this check can no "
                "longer read."
            )
        counts[slug.replace("%20", " ")] = match.group(2)
    return counts


def compare(base_blobs, head_blobs):
    """Return the list of Differences between two revisions' file contents.

    A pure function over two `{path: bytes or None}` mappings — no git, no
    filesystem — so every contract below is testable on strings, and the git
    reader above stays thin enough to cover with one test over a real repo.

    Raises BadgeParseError or SwParseError if a file is present but can no
    longer be parsed. That is deliberate and is never downgraded: advisory
    treatment covers *staleness*, and a parser that has stopped matching is a
    disarmed tripwire, which is a different and worse thing.
    """
    differences = []

    # 1. The stylesheet — whole-file, nothing in it is hand-written.
    base_css, head_css = base_blobs[CSS_PATH], head_blobs[CSS_PATH]
    if base_css != head_css:
        differences.append(Difference(CSS_PATH, whole_file=True))

    # 2. The SW_VERSION value — the rest of sw.js is fair game for a PR.
    base_sw, head_sw = base_blobs[SW_PATH], head_blobs[SW_PATH]
    if (base_sw is None) != (head_sw is None):
        # Added or deleted outright: there is no value to compare, so the
        # whole file is the difference.
        differences.append(Difference(SW_PATH, whole_file=True))
    elif base_sw is not None:
        base_version, _ = stamp_sw_version.parse_sw(base_sw.decode("utf-8"))
        head_version, _ = stamp_sw_version.parse_sw(head_sw.decode("utf-8"))
        if base_version != head_version:
            differences.append(
                Difference(f"{SW_PATH}'s SW_VERSION", base_value=repr(base_version))
            )

    # 3. Each badge count — the rest of README.md is fair game too.
    base_readme, head_readme = base_blobs[README_PATH], head_blobs[README_PATH]
    if (base_readme is None) != (head_readme is None):
        differences.append(Difference(README_PATH, whole_file=True))
    elif base_readme is not None:
        base_counts = _badge_counts(base_readme)
        head_counts = _badge_counts(head_readme)
        for label, base_count in base_counts.items():
            if head_counts[label] != base_count:
                differences.append(
                    Difference(
                        f"{README_PATH}'s {label} badge count",
                        base_value=base_count,
                    )
                )

    return differences


def report(differences, base_sha):
    """Print the optional difference audit. Returns its comparison exit code."""
    if not differences:
        print("Generated output audit: no differences.")
        return 0

    print(
        "Generated output differs between the audited revisions.\n",
        file=sys.stderr,
    )
    for difference in differences:
        print(difference.describe(base_sha), file=sys.stderr)
    print(
        "\nThis is an optional revision-diff audit, not a pull-request gate.\n"
        "Review these changes against their source. This fork requires authors\n"
        "to regenerate and commit output with `make css` and `make badges`;\n"
        "read-only CI checks freshness and does not push fixes.",
        file=sys.stderr,
    )
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base", default="HEAD^1",
        help="the revision to compare against (default: HEAD^1, the base of a "
             "pull_request merge ref)",
    )
    parser.add_argument(
        "--head", default="HEAD",
        help="the revision to check (default: HEAD, the merge result)",
    )
    args = parser.parse_args(argv)

    try:
        base_sha = resolve_rev(args.base)
        base_blobs = read_blobs(args.base)
        head_blobs = read_blobs(args.head)
        differences = compare(base_blobs, head_blobs)
    except GitError as exc:
        print(f"Generated-output check: {exc}", file=sys.stderr)
        return 1
    except (stamp_sw_version.SwParseError, stamp_test_badges.BadgeParseError) as exc:
        print(f"Generated-output check: {exc}", file=sys.stderr)
        return 1

    return report(differences, base_sha)


if __name__ == "__main__":
    sys.exit(main())
