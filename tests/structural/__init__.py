"""Tier 3: source-as-data structural checks.

These parse our own tree and assert claims that nothing else can catch,
because the two halves of each claim live in different files and neither is
wrong on its own. Documentation drift and packaging promises rot silently;
this tier turns "is the sample config still accurate?" into a mechanical
question.
"""
