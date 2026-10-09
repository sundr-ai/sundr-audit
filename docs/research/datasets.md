# Benchmark datasets for decision tasks

Downloaded 2026-10-09. Each dataset lives in its own directory under `data/raw/<name>/`:

```
data/raw/<name>/
  source/            raw files as downloaded (pinned revisions), untouched
  normalized.jsonl   common schema (below)
  meta.json          provenance, license, label set, field mapping, caveats, sha256 of every source file
```

**Common schema** (one JSON object per line):

| field | type | notes |
|---|---|---|
| `id` | str | unique within the file |
| `input` | str | the text to judge |
| `label` | str | gold label, always a member of the label set |
| `labels` | list[str] | the full label set; present on the **first line only** (also `label_set` in `meta.json`) |
| `meta` | dict | dataset-specific extras: `split`, scores, task name, provenance indices |

All downloaded files are treated as data: they are parsed as CSV/TSV/JSON, never executed. The helper scripts
live in `scripts/` and use the standard library only (no `datasets`/`pyarrow`):

- `scripts/fetch_hf_rows.py`: pulls a split through the Hugging Face datasets-server `/rows` JSON API, with
  optional seeded sampling, retry/backoff, an optional page cache, and a hard failure on any truncated cell.
  Used for MT-Bench (parquet-only on HF; no parquet library is installed).
- `scripts/parquet_flat_to_jsonl.py IN.parquet OUT.jsonl [--sample N --seed S]`: a minimal stdlib Parquet reader
  for flat schemas (snappy/gzip/zstd, plain and dictionary encodings). Used for Civil Comments after the `/rows`
  API started returning HTTP 429. Its output was verified against 1,014 rows from the API: identical text and
  identical float32 scores.
- `scripts/normalize_datasets.py <name> data/raw/<name>`: writes `normalized.jsonl` and `meta.json`.

## Summary

| name | task | source | license | normalized rows | labels |
|---|---|---|---|---|---|
| `jev_phishing_bench` | phishing vs legitimate email (agent link-click decision) | HF `AreLit/PhishNChips` v5.2 `core_emails.csv` (via github.com/anisselbd/jev-phishing-bench) | "other": MIT synthetic content + third-party URL terms (one source academic-only) | 2,000 (1,000 / 1,000) | `phishing`, `legitimate` |
| `banking77` | 77-way intent classification | HF `PolyAI/banking77` (CSV from PolyAI GitHub) | CC-BY-4.0 | 13,083 (train 10,003, test 3,080) | 77 intents |
| `civil_comments` | toxicity (binary + score) | HF `google/civil_comments`, seeded test sample | CC0-1.0 | 20,000 (seeded test sample; 18,423 / 1,577) | `non_toxic`, `toxic` (score kept) |
| `mt_bench_human_judgments` | pairwise preference (LLM-as-judge) | HF `lmsys/mt_bench_human_judgments`, `human` split | CC-BY-4.0 (model outputs carry provider terms) | 3,355 votes | `model_a`, `model_b`, `tie` |
| `legalbench` | 3 binary legal tasks | HF `nguha/legalbench` | per task: CC-BY-4.0 x2, CC-BY-SA-4.0 x1 | 4,967 (test 4,947, train 20) | `Yes`, `No` |

Total on disk is about 69 MB (MT-Bench raw JSON 39 MB, Civil Comments sample 19 MB, the rest under 4 MB each).
Network transfer was roughly 100 MB, well under the 300 MB budget. The largest items were the 20.8 MB Civil
Comments test parquet (deleted after sampling) and about 26 MB of MT-Bench `/rows` JSON.

---

## 1. jev_phishing_bench (PhishNChips v5.2)

- **Where the 2,000 emails come from.** They are not in the repo. `prepare_data.py` in
  [anisselbd/jev-phishing-bench](https://github.com/anisselbd/jev-phishing-bench) (commit `1d56e8c`) downloads
  `core_emails.csv` from [AreLit/PhishNChips](https://huggingface.co/datasets/AreLit/PhishNChips) and verifies
  SHA-256 `cebb407f…8204ef`. Our download at HF revision `89afcc39` matches that hash and the v5.2 release manifest.
- **Label field:** `phish_label` (1 = phishing, 0 = legitimate), mapped to `phishing` / `legitimate`, the same mapping
  as jev-phishing-bench.
- **Size / balance:** 2,000 rows, 1,000 / 1,000. Datasources: phishtank 700, tranco 662, cross_domain_expansion_v1
  333, github_phishing_db(_live) 234, openphish(_live) 66, adversarial_legit 5.
- **Normalization:** `input` is the verbatim `email_content` JSON string (sender, from, subject, body,
  link_display_text, link_url), which is exactly what jev-phishing-bench sends to its LLM, so results stay
  comparable to their published numbers. `meta` keeps `url_category`, `datasource`, `strategy`, `model_used`, and
  `jev_order`, their seeded presentation order (`random.Random(20260916).shuffle`). The file is sorted by
  `jev_order`.
- **Also saved:** `prompt_strategies.json` (the 10 system prompts, including the "balanced" one jev uses),
  `reference_results.csv` (published leaderboard), `SOURCE_LICENSES.md`, `V5.2_RELEASE_MANIFEST.md`, `croissant.json`.
  Not downloaded: `benchmark_results.csv` (109 MB grid of 220k model evaluations) and the three auxiliary splits.
- **License:** HF card says `other`. PhishNChips synthetic content and code are MIT. The embedded URL indicators
  carry source terms:
  PhishTank (commercial use allowed per its T&C), OpenPhish (**academic research permission only**), GitHub
  Phishing DB (MIT/CC-BY-4.0), Tranco (academic-use norms, cite NDSS 2019).
- **Caveats:**
  - Contains **real malicious URLs**. Treat them as inert strings: never fetch, render or click them, and run any
    agent under test without network tools.
  - Email bodies are LLM-generated, and ground truth comes from URL reputation feeds. The jev authors show a
    two-feature regex reaches ~91.6% accuracy, so the set largely separates on URL and sender features. Report a
    non-AI baseline next to model results.
  - For commercial use or redistribution, the 66 OpenPhish rows need legal review, or drop them with
    `meta.datasource in {openphish, openphish_live}`.
  - The dataset is from April 2026 and is still "under conference review" (provisional citation only). Re-check
    the license and citation before publishing.

## 2. banking77

- **Source:** [PolyAI/banking77](https://huggingface.co/datasets/PolyAI/banking77) (revision `90d4e2ee`) is
  script-based. HF's datasets-server no longer converts it, so it has no parquet. The CSVs were taken from the
  URLs the HF script points to,
  [PolyAI-LDN/task-specific-datasets@9d08145](https://github.com/PolyAI-LDN/task-specific-datasets/tree/9d081458ff52e53cf7e848f414e6e9344e4e6696/banking_data).
  That is the canonical data, identical to what `load_dataset` used to produce.
- **License:** CC-BY-4.0 (HF card and GitHub LICENSE). Commercial use is fine with attribution (Casanueva et al.
  2020).
- **Size:** train 10,003, test 3,080, 77 labels. Per-label counts range from 75 to 227 across both splits.
- **Normalization:** `input` = `text`, `label` = `category` (string intent). `meta.label_id` is the index in the
  HF `ClassLabel` order, and the label set is listed in that order. ids are `train-00000`… / `test-00000`….
- **Caveats:** there's no validation split, so carve one from train for tuning. Label strings are verbatim,
  including the irregular `Refund_not_showing_up` and `reverted_card_payment?`. Intents are fine-grained and
  partly overlapping, with some reported label noise. The dataset is from 2020 and very widely used, so
  contamination in LLM pre-training data is likely. Report on the official test split for comparability.

## 3. civil_comments

- **Source:** [google/civil_comments](https://huggingface.co/datasets/google/civil_comments) (revision `f2970eb3`).
  Its splits are train 1,804,874 / validation 97,320 / test 97,320, about 420 MB of parquet. The original Google
  Cloud Storage zip now returns 403.
- **Sampling:** a uniform random 20,000 of the 97,320 **test** rows, `random.Random(42).sample(range(97320), 20000)`.
  The test parquet (`data/test-00000-of-00001.parquet`, 20.8 MB, sha256 `410f803f…`) was downloaded with curl and
  read with `scripts/parquet_flat_to_jsonl.py`. Only the sample was kept, as `source/test_sample_20000_seed42.jsonl`
  (all 8 HF columns plus `row_idx`); the parquet was deleted. Its URL and sha256 are in `meta.json`.
- **License:** CC0-1.0 (public domain). Commercial use is fine.
- **Normalization:** `input` = `text`, `label` = `toxic` if `toxicity >= 0.5` else `non_toxic` (the standard
  Jigsaw/WILDS threshold). `meta` keeps `toxicity` (the rater fraction, i.e. the gold score) and the six subtype
  scores (`severe_toxicity`, `obscene`, `threat`, `insult`, `identity_attack`, `sexual_explicit`), plus
  `hf_row_idx`. Label distribution: `non_toxic` 18,423, `toxic` 1,577 (7.9%). No empty texts.
- **Caveats:** heavily imbalanced, so report AUROC/F1/balanced accuracy, or use the score for calibration, not
  raw accuracy. The HF version lacks comment ids, identity-subgroup columns and rater counts, so the Jigsaw
  unintended-bias metrics can't be computed from it; get them from the Kaggle `all_data.csv` if needed. Crowd
  labels have known identity-term bias. The text is offensive. Comments are from 2015–2017 and widely trained on.

## 4. mt_bench_human_judgments

- **Source:** [lmsys/mt_bench_human_judgments](https://huggingface.co/datasets/lmsys/mt_bench_human_judgments)
  (revision `f7d2896d`), fetched via the `/rows` API with no truncation. Both splits are saved raw:
  `source/human.jsonl` (3,355 expert votes) and `source/gpt4_pair.jsonl` (2,400 GPT-4 judge verdicts, with labels
  incl. `tie (inconsistent)`). `source/question.jsonl` comes from FastChat@`b494d0c6` for question categories.
- **License:** CC-BY-4.0 (HF card). The answers were generated by GPT-4, GPT-3.5, Claude-v1, Vicuna-13B,
  Alpaca-13B and LLaMA-13B, so provider terms on using model outputs (e.g. to train competing models) may apply.
  Using them for evaluation is fine; check those terms before using them as training data.
- **Normalization (human split only):** `label` = `winner` (`model_a` / `model_b` / `tie`; counts 1,293 / 1,282 /
  780). `input` is a header naming the judged turn, followed by each assistant's conversation wrapped in MT-Bench's
  pairwise delimiters:
  `<|The Start of Assistant A's Conversation with User|> … ### User: … ### Assistant A: … <|The End of Assistant A's Conversation with User|>`,
  and the same for B. HF rows always store all four messages. For **turn-1** votes only the first exchange is
  included; for **turn-2** votes both exchanges are included, and the header asks about the second answer.
  `meta` holds `question_id`, `category` (8 categories, 10 questions each), `turn`, `model_a`, `model_b`, `judge`
  (65 anonymised annotators) and `hf_row_idx`.
- **Caveats:** each row is **one vote**. There are 2,396 distinct (question, pair, turn) groups, and 282 of them
  have disagreeing votes. Decide between per-vote and majority-aggregated evaluation and state which; the paper
  also reports agreement "without ties". Test judges in both A/B orders, because of position bias. The
  math/coding/reasoning reference answers are not included. The data has been public since 2023, so
  contamination is likely.

## 5. legalbench (3 binary tasks)

- **Source:** [nguha/legalbench](https://huggingface.co/datasets/nguha/legalbench) (revision `daec8237`). Only these
  task files were downloaded: `data/<task>/{train,test}.tsv`, plus `task_metadata.json` and each task's README from
  [HazyResearch/legalbench](https://github.com/HazyResearch/legalbench/tree/main/tasks).
- **Tasks chosen.** All are Yes/No, have ≥ 300 test rows, carry permissive licenses, and come from three different
  upstream sources:

  | task | origin | license | test (Yes/No) | train |
  |---|---|---|---|---|
  | `overruling` | CaseHOLD, US case-law sentences | CC-BY-4.0 | 2,394 (1,216 / 1,178) | 6 |
  | `cuad_audit_rights` | CUAD (Atticus Project), contract clauses | CC-BY-4.0 | 1,216 (608 / 608) | 6 |
  | `definition_classification` | Kevin Tobia, Supreme Court sentences | CC-BY-SA-4.0 | 1,337 (691 / 646) | 8 |

  The suggested `hearsay` (94 test rows) and every `contract_nli_*` task (66–208 test rows) fall short of 300.
  Bigger binary tasks such as `privacy_policy_qa`, `learned_hands_*`, `opp115_*`, `privacy_policy_entailment` and
  `consumer_contracts_qa` are non-commercial (CC-BY-NC*), so they were skipped.
- **Normalization:** a single file covers all three tasks, so **filter or group by `meta.task`**. `input` = `text`
  (the sentence or clause only), `label` = `answer`. Each task's question template (with a `{{text}}` slot) is in
  `meta.json → tasks.<task>.question_template`, and a judge needs it to make sense of Yes/No. `meta.split` is
  `train` for the few-shot examples LegalBench ships and `test` for evaluation. CUAD rows also keep
  `document_name`.
- **Caveats:** the CC-BY-SA task means redistributed derivatives must be share-alike. CUAD negatives are random
  other clauses, which makes the task easier than real clause review. When publishing, cite LegalBench (Guha et
  al. 2023) **and** each task's original source, as the LegalBench README asks.

---

## Commercial use and publishing results: quick matrix

| dataset | commercial eval use | redistribute normalized data | publishing benchmark numbers |
|---|---|---|---|
| jev_phishing_bench | mostly OK; OpenPhish rows academic-only (drop 66 rows or get permission) | avoid; point to HF + pinned sha | OK. Cite PhishNChips (provisional) and jev-phishing-bench if comparing; add a regex baseline |
| banking77 | OK (CC-BY-4.0) | OK with attribution | OK. Use the official test split |
| civil_comments | OK (CC0) | OK | OK. State it is a seeded 20k test sample, not the full test set |
| mt_bench_human_judgments | OK for eval (CC-BY-4.0); model-output terms limit training use | OK with attribution | OK. State per-vote vs aggregated and the tie handling |
| legalbench (3 tasks) | OK (CC-BY / CC-BY-SA) | OK; share-alike for definition_classification | OK. Cite LegalBench and the per-task sources; report per task |

General caveat for publishing: every set except PhishNChips predates current model training cutoffs, so note the
contamination risk. Pin and report the dataset revisions listed in each `meta.json`.

## Using with `sundr bench` / `sundr label`

The CLI reads `id` / `input` / `label` from `normalized.jsonl` and ignores `labels` and `meta`, so the files work
as they are. Two things to watch:

- `sundr bench` takes **one** task spec per dataset file. For LegalBench, write one file per task first,
  e.g. `jq -c 'select(.meta.task=="overruling" and .meta.split=="test")' data/raw/legalbench/normalized.jsonl`.
  Otherwise one spec would be scoring three different Yes/No questions.
- BANKING77 (train + test) and LegalBench (few-shot train + test) mix splits in one file. Filter on `meta.split`
  so you don't evaluate on training rows.

## Reproduce

```bash
curl -L -o /tmp/cc_test.parquet "https://huggingface.co/datasets/google/civil_comments/resolve/f2970eb3a55777454c94069077cc8d9b5866312d/data/test-00000-of-00001.parquet"
python3 -I scripts/parquet_flat_to_jsonl.py /tmp/cc_test.parquet \
  data/raw/civil_comments/source/test_sample_20000_seed42.jsonl --sample 20000 --seed 42
python3 -I scripts/fetch_hf_rows.py --dataset lmsys/mt_bench_human_judgments --split human --page-size 20 \
  --out data/raw/mt_bench_human_judgments/source/human.jsonl
for d in jev_phishing_bench banking77 civil_comments mt_bench_human_judgments legalbench; do
  python3 -I scripts/normalize_datasets.py "$d" "data/raw/$d"
done
```

Plain files (PhishNChips CSV/JSON, BANKING77 CSVs, LegalBench TSVs) were fetched with `curl` from the pinned
revision URLs recorded in each `meta.json`.
