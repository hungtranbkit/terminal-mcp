# Token-efficiency benchmark — measured result

**Verdict: FAIL** — token claim: UNVERIFIED

Corpus: 18 real cases recovered from this repository's git history, 2 synthetic (reported separately).

## What this measurement supports

- The binding constraint is knowledge-map COVERAGE, not the retrieval mechanism: in 11 of 18 real cases the map covers none of the files the fix touched, so no briefing built from it could have named the site.
  - _supports: a coverage finding_
- Where the map does cover the fix site, the briefing named it in 5 of 7 comparable cases (rate 0.714), and was smaller than the luckiest single grep in 2 of 5 (rate 0.4).
  - **UNDERPOWERED: 7 cases. This is not a result, it is a direction worth measuring again on a larger indexed map.**
  - _supports: a narrow, conditional claim_
- The assisted path's success depends on the REPORT naming the module. The same defect, reworded as a user would report it, dropped below the module-choice floor and returned nothing -- so every rate here, measured from commit subjects, is optimistic about real bug reports.
  - _supports: a qualification of every other number in this report_
- No token saving is claimed. No provider reported usage counters for any case in this corpus, and an estimate presented beside measurements would be read as one.
  - _supports: nothing -- it is the absence of a claim_
- Search rounds are the one axis that moves unconditionally: the briefing runs no repository search at all, against 3.94 real greps per case for the unassisted path. That holds whether or not the briefing was useful, which is exactly why it is worth little on its own.
  - _supports: a real but weak claim_

## Methodology, stated before measuring

- No old model is re-run; git's record of each fix is the evidence.
- Baseline = the unassisted locating surface: source files matching terms derived mechanically from the bug's own words, measured by really running git grep at the fix's parent commit.
- The symptom is the fix's commit subject. Its hindsight cuts BOTH ways: it gives the baseline specific terms to grep for, and it gives the assisted path a module name to match on. The synthetic mirror pair measures the second effect directly rather than leaving it assumed.
- The headline compares against the baseline's BEST case -- the single most selective term -- not the union of every term tried.
- The assisted side calls the shipped retrieval and context pack against this repository's real knowledge map and counts what they return.
- The module is chosen from the symptom alone by the shipped scorer; the corpus's fix paths only judge the outcome, never steer it.
- Prior bugs are seeded leave-one-out: no case ever sees itself.
- A briefing that does not contain a real fix path is a MISS, however small.
- Every figure is labelled REAL, ESTIMATE or UNAVAILABLE; nothing unmeasured is filled in.
- The instrument was corrected twice while it was being built, and neither correction improved the result. (1) It chose a module by argmax with no confidence floor, 'choosing' modules on scores of 0.03; applying the shipped threshold turned several of those into no-module-chosen, which cost the assisted side cases it had been credited with. (2) It reused one spec database across cases, so a case could meet its own spec; each case now gets its own. That one changed no number, because the shipped matcher already refuses to match a spec against itself by id -- verified by re-running both ways and diffing, not assumed.

## Headline (real cases only)

| Figure | Value | Label | How it was obtained |
|---|---|---|---|
| mean baseline best surface | 5.17 | ESTIMATE | mean over cases of the most selective term's match count |
| mean assisted surface | 0.61 | REAL | mean over cases of the files the briefing named |
| mean baseline searches | 3.94 | REAL | searches this benchmark really ran per case |
| mean assisted searches | 0 | REAL | the briefing runs no repository search |
| mean seconds (baseline) | 0.05 | REAL | wall clock of the real greps; NOT a worker's own time, which nothing recorded |
| mean seconds (assisted) | 0.08 | REAL | wall clock of the real retrieval + pack build |
| provider usage delta | None | UNAVAILABLE | no provider reported usage counters for any of the 20 benchmark cases -- they predate the telemetry runtime |

Verdicts: {'SHRUNK': 2, 'NO_SHRINK': 3, 'MISSED': 12, 'UNLOCATABLE': 1}

- briefing named the fix site in 5/17 comparable cases (rate 0.294)
- of those, it was smaller than the luckiest single grep in 2/5 (rate 0.4)
- module choice: {'right': 5, 'wrong': 2, 'unmapped': 11}
- re-analysis depth (assisted): {'REUSE_PRIOR_ROOT_CAUSE': 3, 'READ_RELATED': 3, 'FULL_ANALYSIS': 12}

## Where the map covers the fix site (post-hoc split)

_added after the first run, having seen the data; the pre-registered acceptance verdict is unchanged by it_

| Stratum | Cases | Named the fix site | Smaller than the luckiest grep |
|---|---|---|---|
| fix site mapped | 7 | 0.714 (n=7) | 0.4 (n=5) |
| fix site unmapped | 11 | 0.0 (n=10) | None (n=0) |

- **fix site mapped**: the knowledge map covers a file the fix touched, so a briefing could in principle have named it
- **fix site unmapped**: the map covers none of the files the fix touched; this measures coverage, not retrieval

## The same defect, worded two ways

_each pair is one defect described twice. A divergence means the assisted path succeeded on the wording that already named the module and failed on the wording that did not -- so results measured from commit subjects are optimistic about real bug reports._

| Wording | Case | Module chosen | Score | Verdict |
|---|---|---|---|---|
| as committed | real-9970f97 | work_ui | 0.483 | SHRUNK |
| as reported | synthetic-repeat-glyphs | none above the floor | 0.025 | MISSED |

## Per case

| Case | Origin | Category | Baseline best | Assisted | Mapped | Verdict | Analysis |
|---|---|---|---|---|---|---|---|
| real-a25834e | REAL | ui | 4 (`client-side`) | 1 (work_ui) | yes | SHRUNK | REUSE_PRIOR_ROOT_CAUSE |
| real-9970f97 | REAL | ui | 7 (`characters`) | 2 (work_ui) | yes | SHRUNK | REUSE_PRIOR_ROOT_CAUSE |
| real-600c9d7 | REAL | ui | 1 (`mobile`) | 1 (work_ui) | yes | NO_SHRINK | READ_RELATED |
| real-dba2d05 | REAL | ui | 1 (`auto-select`) | 1 (work_ui) | yes | NO_SHRINK | READ_RELATED |
| real-805d644 | REAL | session | 6 (`initial_prompt`) | 0 (no module above the floor) | no | MISSED | FULL_ANALYSIS |
| real-8f9d03a | REAL | session | 9 (`intentionally`) | 0 (no module above the floor) | no | MISSED | FULL_ANALYSIS |
| real-4d51782 | REAL | session | 1 (`collision`) | 0 (no module above the floor) | no | MISSED | FULL_ANALYSIS |
| real-c6e2e63 | REAL | auth | 1 (`server-hub`) | 0 (no module above the floor) | yes | MISSED | FULL_ANALYSIS |
| real-5588feb | REAL | auth | 4 (`listing`) | 0 (no module above the floor) | yes | MISSED | FULL_ANALYSIS |
| real-8fca313 | REAL | auth | 2 (`LaunchAgent`) | 0 (no module above the floor) | no | MISSED | FULL_ANALYSIS |
| real-998ab63 | REAL | backend | 2 (`queue.db`) | 2 (queue) | yes | NO_SHRINK | FULL_ANALYSIS |
| real-32c77ac | REAL | backend | 1 (`clobbered`) | 0 (no module above the floor) | no | MISSED | FULL_ANALYSIS |
| real-d160eb0 | REAL | backend | 29 (`register`) | 0 (no module above the floor) | no | MISSED | FULL_ANALYSIS |
| real-b107c94 | REAL | simple_logic | 4 (`completion-marker`) | 3 (queue) | no | MISSED | REUSE_PRIOR_ROOT_CAUSE |
| real-fe53f97 | REAL | simple_logic | 3 (`TARGET_AWAITING_APPROVAL`) | 0 (no module above the floor) | no | MISSED | FULL_ANALYSIS |
| real-12d41d4 | REAL | simple_logic | 6 (`DELIVERY_UNKNOWN`) | 0 (no module above the floor) | no | MISSED | FULL_ANALYSIS |
| real-184b7e0 | REAL | unknown | 10 (`restarts`) | 1 (work_ui) | no | UNLOCATABLE | READ_RELATED |
| real-4a3ca43 | REAL | unknown | 2 (`composer-swallow`) | 0 (no module above the floor) | no | MISSED | FULL_ANALYSIS |
| synthetic-repeat-glyphs | SYNTHETIC | ui | 2 (`drawing`) | 0 (no module above the floor) | yes | MISSED | FULL_ANALYSIS |
| synthetic-unmapped-bridge | SYNTHETIC | unknown | 3 (`ask-ChatGPT`) | 0 (no module above the floor) | no | MISSED | FULL_ANALYSIS |

## Acceptance

The bar, registered before the run and published with every result: `located_rate = 0.5`, `min_real_cases = 10`, `shrunk_rate = 0.5`.

- **sample_size**: MET (18 vs required 10)
- **located_rate**: NOT_MET (0.294 vs required 0.5, n=17)
- **shrunk_rate**: NOT_MET (0.4 vs required 0.5, n=5)

- **token claim**: UNVERIFIED — no provider reported usage counters for any of the 20 benchmark cases -- they predate the telemetry runtime

---

Generated by `scripts/benchmark/run_tokeff_benchmark.py` from `benchmarks/tokeff_corpus.json`; the machine-readable result, including every per-case measurement, is `benchmarks/tokeff_result.json`. This file is generated — edit the harness or the corpus, not the report.
