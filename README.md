# B2B Lead Discovery Agent

A Slack-native agent that finds companies that **need a custom audio/voice device built but have no factory** (microphones, earbuds, wearables with mics, conference speakers, recorders) by reading public LinkedIn posts and company pages, scores them, finds a reachable contact, and drafts first-touch messages with the salesperson in the loop.

> Built for a small hardware OEM/ODM company and used by its business-development team. This public copy replaces the company with **Acme Devices** and every real company and person in fixtures with fictional ones; the collected data, labels and live probe samples are not included.

---

## The problem it solves

Searching LinkedIn for "need a manufacturer for our headphones" returns mostly the opposite of what you want: factories and trading companies advertising, using the same words (*prototype, tooling, mass production, MOQ*). A human reading posts got fooled by factory SEO posts more than once. The design is built around that.

## Architecture: four layers, enforced

```
triggers/            entry points: a daily scheduled run, and LangGraph conversations in Slack
query_intelligence/  what to search for: query repository, stats, selection, planning
discovery/           the shared core — harvest → normalize → prefilter → analyze. Pure, no DB, no Slack
persistence/         the only place that knows SQL (Postgres, 13 migrations)
delivery/            the only place that imports Slack (cards, buttons, conversations)
adapters/            the only place that knows a data vendor's field names (Apify / HarvestAPI actors)
models/              shared types; standard library only
```

`scripts/check_isolation.py` enforces the rules with static analysis (`ast`), not by "it happens not to be installed":
the core may not import Slack, may not know vendor fields (data vendors disappear — one was shut down by a lawsuit and its customers lost access overnight), and may not touch the database, so the same inputs always give the same output and historical data can be replayed as a regression set.

The scheduled run and the Slack conversations call **the same** `discovery/pipeline.py` function.

## Cheapest gate first

```
harvest       ~$2 per 1k items     volume is chosen by us
normalize     $0                   resolve who the company actually is (three hops: post → author → company)
prefilter     $0                   rule gates: suppliers, big brands, off-topic
cooldown      $0                   companies already excluded never reach a model again
analyzer      paid, and can be wrong      ← last
```

The order matters less for cost than for precision: rules can drop a factory's SEO post with certainty; a model can only drop it with some probability. **What can be decided deterministically is never left to the model.** The analyzer itself must quote the post for every claim; a quote that is not in the source text is flagged as fabricated and the assessment is rejected.

## Slack conversations (LangGraph)

Two graphs run inside Slack threads, persisted with a Postgres checkpointer so a conversation can be resumed days later:

**Query graph** — "find podcast-mic startups that posted in the last two weeks":
understand the request → propose search queries and a cost estimate → *wait for the user to approve or edit* → check the monthly spending cap → harvest → push lead cards.

**Drafting graph** — on a lead card, "write to them":
retrieve what we know → plan the drafts (who, which channel, which angle) → *wait for approval* → write each draft (model + code checks, rewrite on failure) → *wait for feedback* → split one reply into per-draft verdicts → record every round in `draft_revisions` → loop until each draft is final or dropped.

Design choices that came from real use:
- Every draft has a stable number; changing the plan keeps numbers, so "make #2 shorter" always means the same draft.
- Interrupts are the first statement of their node and a node has at most one, because LangGraph replays the node on resume.
- A monthly budget cap (with a configurable billing day) is checked before any paid harvest; a run that would exceed it is refused with the numbers shown.

## Finding a real contact

Authors of posts are often not employees of the company they write about (agencies, investors, journalists). `discovery/authorship.py` classifies each author against the company — company page, named in headline, current job, partial name match, third party — and only people who plausibly work there are looked up. Websites on shared platforms (Shopify, Wix, Linktree …) are not taken as the company's domain. Contact lookups are optional steps: one vendor failing skips that step and the rest still run.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q                       # 1026 tests; 47 are skipped in this public copy (see below)
python scripts/check_isolation.py
```

- Fakes stand in for every model and vendor call; a guard fails any test that tries a real call.
- Graph tests drive full Slack conversations through the interrupts, including restarts and crashes mid-run.
- The 47 skipped tests replay raw vendor responses captured from real lookups (real people and websites), which are not published.

## Status

Scheduled discovery, Slack cards and feedback, the query graph, contact finding and the drafting graph are done and were used in production. Remaining: a nightly memory processor that learns which queries pay off, and deployment hardening.

## Notes

- Code comments and prompts are mostly in Chinese (the team worked in Chinese); identifiers and this README are in English. Some comments mention scripts that were removed from this copy because they depended on the private data set.
- Collecting public LinkedIn content must respect LinkedIn's terms and local law; this repository contains no collected data.

## Stack

Python 3.12 · LangGraph (Postgres checkpointer) · OpenAI · Slack Bolt (Socket Mode) · Apify / HarvestAPI actors · PostgreSQL (Neon) · psycopg 3
