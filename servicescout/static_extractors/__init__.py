"""Deterministic cross-checkers for LLM-extracted catalog facts.

These modules verify the evidence the LLM cites against the actual source
files, so confidence on each fact reflects how well it grounds out in the
repo rather than how confident the model felt. See Epic #9 Tier 1 #1.
"""
