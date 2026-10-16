"""
Classification dataset curation — ported from OASIS and made multi-user.

A curation project holds point annotations (from CoralNet regional CSVs, or
added by hand) as items with an original label, a current label, a classifier
suggestion and review state. The interactive confusion matrix compares current
(or original) labels against the classifier and drills into the disagreeing
items, where curators relabel, include/exclude and review them.

What changed from OASIS, and why:

* Every change records the signed-in user (``auth``), not a free-text name the
  browser sent — OASIS stamped the project creator's name on everyone's edits.
* Every change bumps the item's ``revision`` and is checked against the
  revision the editor saw, so two people cannot silently overwrite each other;
  quality review and bulk edits included, which OASIS let through.
* It runs on Oracle as well as SQLite: no JSON-in-SQL, no ``group_concat``, no
  SQLite upserts, and strings are always sized (see ``appdb.base``).
"""
