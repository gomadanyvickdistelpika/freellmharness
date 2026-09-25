"""A fictional test user, so the privacy tests have something real to protect.

Nothing here is about a real person. The public repo ships only {{templates}};
the tests write this filled-in profile into their own temporary data folder.
"""

DEMO = {
    "about": ("public", "# About me\nName: Alex Example\nLocation: Springfield, Examplestan\n"
                        "Summary: Support engineer learning to build AI agents."),
    "working-style": ("public", "# How to work with me\n- Short answers, one step at a time."),
    "job-search": ("personal", "# Job search\n- Target: support engineer, remote\n"
                               "- Minimum salary: 40,000 EXD"),
    "family": ("private", "# Family\n- Household: Alex and partner\n- Children: Robin (8), loves dinosaurs"),
}


def fill() -> None:
    from aegis import personal
    for name, (tier, body) in DEMO.items():
        personal.save(name, f"---\ntitle: {name}\ntier: {tier}\ntopics: {name.replace('-', ' ')}, "
                            f"{'salary job search' if name == 'job-search' else ''}"
                            f"{'children kids family' if name == 'family' else ''}\n---\n{body}\n")
