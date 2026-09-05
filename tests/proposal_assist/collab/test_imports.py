def test_deps_import():
    import markdown_it  # noqa: F401
    import prosemirror  # noqa: F401
    import pycrdt  # noqa: F401

    from grant_lab.proposal_assist import collab  # noqa: F401
