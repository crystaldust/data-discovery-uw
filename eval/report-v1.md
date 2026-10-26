# Discovery accuracy evaluation

Built by `uv run ddm eval-report` from `eval/labels.csv` and `eval/strata.json`; labelling rules in `eval/LABELING.md`.

Pairs: 200. Unsure: 10. Not labelled yet: 0. Sampling seed: `ddm\-phase\-4`. Review threshold when sampled: 0.6.

## Automatic merges on strong identifiers

Precision: the share of automatic merges that join two feeds of the same podcast.

| Stratum | Population | Labelled | Precision | 95% interval |
|---|---|---|---|---|
| strong_same_title | 646 | 46 | 97.8% | 88.7%–99.6% |
| strong_diff_title | 63 | 34 | 44.1% | 28.9%–60.5% |
| Weighted (both strata) | 709 | 80 | 93.1% | 87.2%–98.9% |

## Strong-identifier conflicts sent to review

The share of conflicting pairs that are nonetheless the same podcast.

| Stratum | Population | Labelled | Same | 95% interval |
|---|---|---|---|---|
| strong_conflict | 117 | 10 | 70.0% | 39.7%–89.2% |

## Fuzzy candidates by score band

Each discovered record's best fuzzy neighbour; precision is the share that is the same podcast.

| Stratum | Scores | Population | Labelled | Precision | 95% interval |
|---|---|---|---|---|---|
| fuzzy_0.0 | 0.0–0.3 | 11120 | 15 | 0.0% | 0.0%–20.4% |
| fuzzy_0.3 | 0.3–0.4 | 5097 | 15 | 0.0% | 0.0%–20.4% |
| fuzzy_0.4 | 0.4–0.5 | 1379 | 14 | 0.0% | 0.0%–21.5% |
| fuzzy_0.5 | 0.5–0.6 | 631 | 14 | 7.1% | 1.3%–31.5% |
| fuzzy_0.6 | 0.6–0.7 | 76 | 14 | 14.3% | 4.0%–39.9% |
| fuzzy_0.7 | 0.7–0.8 | 87 | 14 | 64.3% | 38.8%–83.7% |
| fuzzy_0.8 | 0.8–1.0 | 499 | 14 | 71.4% | 45.4%–88.3% |

## Threshold table

Queue precision and recall if the review threshold were at each band edge. Recall is relative to true duplicates whose best neighbour's title is at least 0.3 similar.

| Threshold | Precision | Recall | Precision 95% | Recall 95% |
|---|---|---|---|---|
| 0.4 | 17.5% | 100.0% | 8.1%–26.9% | 7.6%–100.0% |
| 0.5 | 36.2% | 100.0% | 24.4%–48.1% | 7.0%–100.0% |
| 0.6 | 63.9% | 90.4% | 47.4%–80.5% | 6.5%–98.5% |
| 0.7 | 70.4% | 88.1% | 51.8%–88.9% | 6.4%–97.9% |
| 0.8 | 71.4% | 76.1% | 50.0%–92.9% | 5.5%–90.8% |

## Recommended threshold

**0.6**: the lowest threshold whose estimated queue precision is at least 50.0%, which keeps as much recall as possible while at least half of what reviewers see is a real duplicate.

## What this evaluation cannot see

- Duplicates whose titles share less than 0.3 trigram similarity never become candidates, so they are missing from both the queue and this recall estimate.
- Two new feeds of the same podcast that arrive in the same run are not compared by the fuzzy matcher (strong identifiers still join them).
- 'rename_or_move' is checked here only as 'same podcast'; whether the old feed was retired or still runs as a mirror is not labelled.

## Real examples

**rename_or_move**

- It's not Just the Numbers ([feed](https://podcastindex.org/podcast/7905727)) ~ Strategy and the Virtual Controller ([feed](https://podcastindex.org/podcast/3930649))
- CISO Unplugged ([feed](https://podcastindex.org/podcast/7607568)) ~ GRC & Cyber Security Podcast ([feed](https://podcastindex.org/podcast/4921140))
- Plus Feed ([feed](https://podcastindex.org/podcast/7407997)) ~ Darknet Diaries ([feed](https://podcastindex.org/podcast/577105))

**duplicate**

- Copy Kit ([feed](https://podcastindex.org/podcast/7879601)) ~ Tried It: Your Proven Pathway to a Better Copywriting Business ([feed](https://podcastindex.org/podcast/7028566))
- الذكاء الاصطناعي لمهندسي المياه ([feed](https://podcastindex.org/podcast/8059085)) ~ AI for Water Engineers \(العربية\) ([feed](https://podcastindex.org/podcast/8058264))
- Leadership Daily ([feed](https://podcastindex.org/podcast/7005337)) ~ 🎧 Leadership Daily ([feed](https://podcastindex.org/podcast/6949858))

**uncertain (a real duplicate)**

- Metta Talks ([feed](https://podcastindex.org/podcast/7749903)) ~ Metta Talks ([feed](https://podcastindex.org/podcast/3953634))
- Kubernetes Center of Excellence ([feed](https://podcastindex.org/podcast/6864051)) ~ Kubernetes Center of Excellence Podcast ([feed](https://podcastindex.org/podcast/5782981))
- Unpacked ([feed](https://podcastindex.org/podcast/6781572)) ~ Unpacked AI ([feed](https://podcastindex.org/podcast/6580646))

**new (a look-alike, not a duplicate)**

- Coffee \+ SEO ([feed](https://podcastindex.org/podcast/7191385)) ~ Coffee & Chai ([feed](https://podcastindex.org/podcast/698812))
- WHAT THE PR\!V\*CY ([feed](https://podcastindex.org/podcast/6782247)) ~ What The Pat? ([feed](https://podcastindex.org/podcast/336905))
- A.I 101 ([feed](https://podcastindex.org/podcast/7818408)) ~ AIM 101 ([feed](https://podcastindex.org/podcast/5019523))
