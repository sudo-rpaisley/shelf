#!/usr/bin/env python3
"""Generated-artifact checks are mandatory locally and on every CI event.

This fork accepts author-generated CSS, service-worker digests and badges in
pull requests. CI verifies the submitted bytes with a read-only token; it never
repairs them with an automatic commit. Keep one predicate for existing callers.
"""
import sys


def staleness_is_enforceable() -> bool:
    """The author can and must regenerate artifacts before every submission."""
    return True


def main():
    if staleness_is_enforceable():
        print("staleness is enforceable in this context")
        return 0
    print("staleness is advisory-only in this context (pull_request build)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
