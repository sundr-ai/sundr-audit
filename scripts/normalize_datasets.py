#!/usr/bin/env python3
"""Normalise downloaded benchmark datasets into a common JSONL schema.

Each output line: {"id": str, "input": str, "label": str, "meta": {...}}; the first
line additionally carries "labels" (the full label set). A sidecar meta.json records
provenance, license, label set, field mapping and caveats.

Downloaded files are treated as data only: they are parsed (CSV/TSV/JSON), never executed.

Usage:
  python3 -I scripts/normalize_datasets.py <dataset> <raw_dir>
  dataset in: jev_phishing_bench, banking77, civil_comments, mt_bench_human_judgments, legalbench
"""

from __future__ import annotations

import collections
import csv
import hashlib
import json
import random
import re
import sys
from pathlib import Path

DOWNLOADED = "2026-10-09"
csv.field_size_limit(1 << 30)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def source_files(raw: Path) -> dict:
    src = raw / "source"
    return {
        str(p.relative_to(raw)): {"bytes": p.stat().st_size, "sha256": sha256(p)}
        for p in sorted(src.rglob("*"))
        if p.is_file()
    }


def write_outputs(raw: Path, rows: list[dict], labels: list[str], meta: dict) -> None:
    seen = set()
    for r in rows:
        assert isinstance(r["id"], str) and r["id"] not in seen, r["id"]
        seen.add(r["id"])
        assert isinstance(r["input"], str) and r["input"].strip(), r["id"]
        assert r["label"] in labels, (r["id"], r["label"])
    with (raw / "normalized.jsonl").open("w", encoding="utf-8") as f:
        for i, r in enumerate(rows):
            out = {"id": r["id"], "input": r["input"], "label": r["label"]}
            if i == 0:
                out["labels"] = labels
            out["meta"] = r.get("meta", {})
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
    dist = collections.Counter(r["label"] for r in rows)
    splits = collections.Counter(r.get("meta", {}).get("split", "all") for r in rows)
    meta = {
        **meta,
        "row_count": len(rows),
        "split_counts": dict(splits),
        "label_set": labels,
        "label_distribution": {l: dist.get(l, 0) for l in labels},
        "normalized_file": "normalized.jsonl",
        "normalized_schema": {
            "id": "str, unique within file",
            "input": "str, text to judge",
            "label": "str, gold label (member of label_set)",
            "labels": "list[str], full label set; present on the FIRST line only",
            "meta": "dict, dataset-specific extras (split, scores, provenance)",
        },
        "downloaded": DOWNLOADED,
        "source_files": source_files(raw),
    }
    (raw / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{raw.name}: {len(rows)} rows, {len(labels)} labels, dist={dict(dist) if len(labels) < 10 else '...'}")


# --------------------------------------------------------------------------- jev / PhishNChips
def jev_phishing_bench(raw: Path) -> None:
    src = raw / "source" / "core_emails.csv"
    expected = "cebb407ff8630491a97400e37464b8db8dfc4299164fca51fcb4ac7eec8204ef"
    assert sha256(src) == expected, "core_emails.csv checksum mismatch vs jev-phishing-bench / v5.2 manifest"
    with src.open(encoding="utf-8", newline="") as f:
        rows_in = list(csv.DictReader(f))
    assert len(rows_in) == 2000
    # Reproduce jev-phishing-bench's seeded presentation order (prepare_data.py, SEED=20260916).
    order_ids = [r["id"] for r in rows_in]
    random.Random(20260916).shuffle(order_ids)
    jev_order = {rid: i for i, rid in enumerate(order_ids)}
    rows = []
    for r in rows_in:
        email = json.loads(r["email_content"])  # validate it parses; input stays verbatim
        assert {"sender", "from", "subject", "body", "link_display_text", "link_url"} <= email.keys()
        y = int(r["phish_label"])
        rows.append(
            {
                "id": r["id"],
                "input": r["email_content"],
                "label": "phishing" if y == 1 else "legitimate",
                "meta": {
                    "split": "core",
                    "phish_label": y,
                    "url_category": r["url_category"],
                    "datasource": r["datasource"],
                    "strategy": r["strategy"],
                    "model_used": r["model_used"],
                    "jev_order": jev_order[r["id"]],
                },
            }
        )
    rows.sort(key=lambda r: r["meta"]["jev_order"])
    write_outputs(
        raw,
        rows,
        ["phishing", "legitimate"],
        {
            "name": "jev_phishing_bench (PhishNChips v5.2 core_emails)",
            "task": "binary phishing detection on email agent inputs (should the agent click the link?)",
            "benchmark_repo": "https://github.com/anisselbd/jev-phishing-bench",
            "benchmark_repo_commit": "1d56e8c64d029a9554a0874e2ef2901ed196e230",
            "data_origin": "Not in the repo: prepare_data.py downloads PhishNChips v5.2 core_emails.csv from Hugging Face "
            "(AreLit/PhishNChips) and checks it against SHA-256 cebb407f...8204ef. The 2,000 emails are exactly that file.",
            "source_url": "https://huggingface.co/datasets/AreLit/PhishNChips",
            "source_file_url": "https://huggingface.co/datasets/AreLit/PhishNChips/resolve/"
            "89afcc39610084298c4679159cb2e27d9ffffa46/core_emails.csv",
            "source_revision": "89afcc39610084298c4679159cb2e27d9ffffa46",
            "license": "other (HF card). MIT for PhishNChips synthetic email content and code; embedded URL indicators "
            "carry third-party terms: PhishTank (commercial use allowed per its T&C), OpenPhish (academic research "
            "permission only), GitHub Phishing DB (MIT/CC-BY-4.0), Tranco (academic-use norms, cite NDSS 2019). "
            "See source/SOURCE_LICENSES.md.",
            "label_field": "phish_label (1 = phishing, 0 = legitimate)",
            "field_mapping": {
                "id": "id",
                "input": "email_content (verbatim JSON string with sender, from, subject, body, link_display_text, "
                "link_url; the same string jev-phishing-bench sends to the LLM as email_raw)",
                "label": "phish_label: 1 -> 'phishing', 0 -> 'legitimate' (same mapping as jev-phishing-bench)",
                "meta.url_category/datasource/strategy/model_used": "same-named CSV columns",
                "meta.jev_order": "position in jev-phishing-bench's seeded shuffle (random.Random(20260916).shuffle "
                "over CSV order); file is sorted by it. Reproduced here, not checked against their emails.jsonl.",
            },
            "not_downloaded": "benchmark_results.csv (109 MB grid of 220k model evaluations) and the auxiliary splits "
            "(cross_domain_legitimate_v5, infrastructure_phishing_expanded, real_phishing_validation).",
            "caveats": [
                "Contains REAL malicious URLs (PhishTank/OpenPhish/GitHub Phishing DB). Treat as inert strings: never "
                "fetch, render or click them, and keep any agent under test away from network tools.",
                "Email bodies are LLM-generated; the phishing signal sits mostly in the URL/sender, and the "
                "jev-phishing-bench authors report a two-feature regex reaches ~91.6% accuracy, so the set largely "
                "separates by construction.",
                "Ground truth comes from URL reputation feeds, not human review of each email.",
                "OpenPhish rows (66) were cleared for academic research only. Get legal review before commercial use "
                "or redistribution, or drop datasource in {openphish, openphish_live}.",
                "Released April 2026 (post-dates most model training cutoffs). Contamination risk will grow over time.",
            ],
        },
    )


# --------------------------------------------------------------------------- BANKING77
def banking77(raw: Path) -> None:
    script = (raw / "source" / "hf_banking77.py.txt").read_text(encoding="utf-8")
    block = script.split("names=[", 1)[1].split("]", 1)[0]
    labels = re.findall(r'"([^"]+)"', block)
    assert len(labels) == 77, len(labels)
    rows = []
    for split in ("train", "test"):
        with (raw / "source" / f"{split}.csv").open(encoding="utf-8", newline="") as f:
            reader = csv.reader(f, quotechar='"', delimiter=",", quoting=csv.QUOTE_ALL, skipinitialspace=True)
            header = next(reader)
            assert header == ["text", "category"], header
            for i, (text, cat) in enumerate(reader):
                rows.append(
                    {
                        "id": f"{split}-{i:05d}",
                        "input": text,
                        "label": cat,
                        "meta": {"split": split, "label_id": labels.index(cat)},
                    }
                )
    assert {r["label"] for r in rows} == set(labels)
    write_outputs(
        raw,
        rows,
        labels,
        {
            "name": "BANKING77",
            "task": "77-way intent classification of single-turn online-banking customer queries",
            "source_url": "https://huggingface.co/datasets/PolyAI/banking77",
            "source_revision": "90d4e2ee5521c04fc1488f065b8b083658768c57",
            "raw_data_url": "https://github.com/PolyAI-LDN/task-specific-datasets/tree/"
            "9d081458ff52e53cf7e848f414e6e9344e4e6696/banking_data",
            "download_note": "The HF repo is script-based (banking77.py), which the datasets-server no longer converts, "
            "so it has no parquet. The CSVs were downloaded from the PolyAI GitHub URLs that the HF script points to "
            "(pinned commit). Label order follows the script's ClassLabel names.",
            "license": "CC-BY-4.0 (HF card and GitHub repo LICENSE)",
            "field_mapping": {
                "id": "'<split>-<0-based row index in CSV>'",
                "input": "text",
                "label": "category (string intent name)",
                "meta.label_id": "index of category in HF ClassLabel order (0-76)",
                "meta.split": "train | test",
            },
            "caveats": [
                "Label strings are verbatim, including the irregular 'Refund_not_showing_up' and "
                "'reverted_card_payment?'.",
                "No official validation split. Carve one out of train if you tune anything.",
                "Fine-grained, partly overlapping intents (e.g. card_arrival vs card_delivery_estimate), and the "
                "literature reports some label noise.",
                "Published in 2020 and very widely used, so expect contamination in LLM pre-training data.",
            ],
        },
    )


# --------------------------------------------------------------------------- Civil Comments
def civil_comments(raw: Path) -> None:
    src = raw / "source" / "test_sample_20000_seed42.jsonl"
    score_keys = ["toxicity", "severe_toxicity", "obscene", "threat", "insult", "identity_attack", "sexual_explicit"]
    rows = []
    with src.open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            tox = float(r["toxicity"])
            rows.append(
                {
                    "id": f"test-{r['row_idx']}",
                    "input": r["text"],
                    "label": "toxic" if tox >= 0.5 else "non_toxic",
                    "meta": {"split": "test", "hf_row_idx": r["row_idx"], **{k: r[k] for k in score_keys}},
                }
            )
    assert len(rows) == 20000
    empty = [r["id"] for r in rows if not r["input"].strip()]
    rows = [r for r in rows if r["input"].strip()]
    write_outputs(
        raw,
        rows,
        ["non_toxic", "toxic"],
        {
            "name": "Civil Comments (Jigsaw Unintended Bias), seeded test-split sample",
            "task": "toxicity classification of public news-site comments (score kept for regression/calibration)",
            "source_url": "https://huggingface.co/datasets/google/civil_comments",
            "source_revision": "f2970eb3a55777454c94069077cc8d9b5866312d",
            "download_method": "curl of the HF test parquet (pinned revision), then the stdlib reader "
            "scripts/parquet_flat_to_jsonl.py IN.parquet source/test_sample_20000_seed42.jsonl --sample 20000 --seed 42. "
            "The parquet was deleted after extraction to keep only the sample. Reader output was cross-checked against "
            "1,014 of the same rows fetched via the datasets-server /rows API: identical text, identical float32 "
            "scores. (A pure /rows API fetch was attempted first and hit HTTP 429 rate limits.)",
            "source_parquet": {
                "url": "https://huggingface.co/datasets/google/civil_comments/resolve/"
                "f2970eb3a55777454c94069077cc8d9b5866312d/data/test-00000-of-00001.parquet",
                "bytes": 20800265,
                "sha256": "410f803ff161e7c4ba005db7de5b444cac413f4861bf056b0fd69d670d75aa08",
                "kept_locally": False,
            },
            "sampling": "uniform random 20,000 of the 97,320 HF test rows: random.Random(42).sample(range(97320), "
            "20000). Original HF splits: train 1,804,874 / validation 97,320 / test 97,320.",
            "dropped_empty_text_ids": empty,
            "license": "CC0-1.0 (HF card; Civil Comments corpus released to the public domain)",
            "field_mapping": {
                "id": "'test-<row index in HF test split>'",
                "input": "text",
                "label": "toxicity >= 0.5 -> 'toxic' else 'non_toxic' (the standard Jigsaw/WILDS threshold)",
                "meta.toxicity": "fraction of raters who labelled the comment toxic (float in [0, 1]); the gold score",
                "meta.severe_toxicity/obscene/threat/insult/identity_attack/sexual_explicit": "rater-fraction subtype "
                "scores",
                "meta.hf_row_idx": "row index in the HF test split at the pinned revision",
            },
            "caveats": [
                "The HF version drops the comment id, the per-identity annotation columns and the rater counts that "
                "are in the original Kaggle all_data.csv, so identity-subgroup bias metrics cannot be computed from "
                "this file.",
                "Heavy class imbalance (about 8% toxic at the 0.5 threshold). Report AUROC/F1 or a balanced metric, "
                "not raw accuracy.",
                "Labels are crowd-rater fractions and carry known annotator bias (e.g. identity terms over-flagged).",
                "Content includes offensive, profane and identity-attacking text.",
                "2015-2017 comments, widely used in training data, so contamination is likely.",
            ],
        },
    )


# --------------------------------------------------------------------------- MT-Bench human judgments
def _render_conv(conv: list[dict], turns: int, who: str) -> str:
    msgs = conv[: 2 * turns]
    assert [m["role"] for m in msgs] == ["user", "assistant"] * turns, [m["role"] for m in msgs]
    parts = [f"<|The Start of Assistant {who}'s Conversation with User|>"]
    for m in msgs:
        role = "User" if m["role"] == "user" else f"Assistant {who}"
        parts.append(f"### {role}:\n{m['content']}")
    parts.append(f"<|The End of Assistant {who}'s Conversation with User|>")
    return "\n\n".join(parts)


def mt_bench_human_judgments(raw: Path) -> None:
    questions = {}
    with (raw / "source" / "question.jsonl").open(encoding="utf-8") as f:
        for line in f:
            q = json.loads(line)
            questions[q["question_id"]] = q
    rows = []
    with (raw / "source" / "human.jsonl").open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            turn = int(r["turn"])
            ca, cb = r["conversation_a"], r["conversation_b"]
            # Sanity: both sides answer the same user turns.
            for k in range(2 * turn):
                if ca[k]["role"] == "user":
                    assert ca[k]["content"] == cb[k]["content"], r["row_idx"]
            if turn == 1:
                header = ("[Pairwise judgment, turn 1] Which assistant answered the user's question better? "
                          "Answer model_a (Assistant A), model_b (Assistant B) or tie.")
            else:
                header = ("[Pairwise judgment, turn 2] Given the two-turn conversations below, which assistant "
                          "answered the user's SECOND question better? Answer model_a (Assistant A), model_b "
                          "(Assistant B) or tie.")
            text = "\n\n".join([header, _render_conv(ca, turn, "A"), _render_conv(cb, turn, "B")])
            q = questions.get(r["question_id"], {})
            rows.append(
                {
                    "id": f"human-{r['row_idx']}",
                    "input": text,
                    "label": r["winner"],
                    "meta": {
                        "split": "human",
                        "hf_row_idx": r["row_idx"],
                        "question_id": r["question_id"],
                        "category": q.get("category"),
                        "turn": turn,
                        "model_a": r["model_a"],
                        "model_b": r["model_b"],
                        "judge": r["judge"],
                    },
                }
            )
    groups = collections.defaultdict(set)
    for r in rows:
        m = r["meta"]
        groups[(m["question_id"], m["model_a"], m["model_b"], m["turn"])].add(r["label"])
    n_multi = sum(1 for k, v in groups.items() if len(v) > 1)
    write_outputs(
        raw,
        rows,
        ["model_a", "model_b", "tie"],
        {
            "name": "MT-Bench human pairwise judgments",
            "task": "pairwise preference judgment (LLM-as-judge vs expert humans) over two assistants' answers to "
            "MT-Bench questions",
            "source_url": "https://huggingface.co/datasets/lmsys/mt_bench_human_judgments",
            "source_revision": "f7d2896d2cc5d80f8b55c2bbc722613555233c25",
            "download_method": "datasets-server /rows JSON API (pages of 20, every row checked for truncated cells). "
            "Both HF splits saved raw: source/human.jsonl (3,355) and source/gpt4_pair.jsonl (2,400 GPT-4 judge "
            "verdicts, labels model_a/model_b/tie/'tie (inconsistent)'). Only the human split is normalised.",
            "question_metadata": "source/question.jsonl from lm-sys/FastChat@b494d0c6 (fastchat/llm_judge/data/"
            "mt_bench), used for meta.category",
            "license": "CC-BY-4.0 (HF card)",
            "field_mapping": {
                "id": "'human-<row index in HF human split>'",
                "input": "header line naming the judged turn, then each assistant's conversation wrapped in "
                "<|The Start of Assistant A's Conversation with User|> ... <|The End of ...|> blocks (MT-Bench "
                "pairwise judge-prompt delimiters). Turn-1 rows include only the first user question and answer, even "
                "though the HF row stores all 4 messages. Turn-2 rows include both turns.",
                "label": "winner (model_a | model_b | tie)",
                "meta": "question_id, category, turn, model_a, model_b, judge (anonymised annotator id), hf_row_idx",
            },
            "stats": {
                "distinct_question_pair_turn_groups": len(groups),
                "groups_with_disagreeing_votes": n_multi,
                "n_judges": len({r["meta"]["judge"] for r in rows}),
            },
            "caveats": [
                "Each row is one annotator vote. The same (question, model pair, turn) can appear several times with "
                "different winners. Either evaluate per vote or aggregate by majority, and say which. The original "
                "paper's agreement metric also has a 'without ties' variant.",
                "Position bias: A/B order is as presented to annotators. Judge evaluations should also test swapped "
                "order.",
                "Answers are from 2023-era models (GPT-4, GPT-3.5, Claude-v1, Vicuna-13B, Alpaca-13B, LLaMA-13B). "
                "Responses generated by OpenAI/Anthropic models may carry provider terms on using outputs to train "
                "competing models. Fine for evaluation, but check terms before using them as training data.",
                "Math/coding/reasoning questions have reference answers in FastChat (not included). The original GPT-4 "
                "judge used them for those categories.",
                "MT-Bench questions and these judgments are public since mid-2023, so contamination is likely.",
            ],
        },
    )


# --------------------------------------------------------------------------- LegalBench
LB_TASKS = {
    "overruling": {"license": "CC-BY-4.0", "origin": "CaseHOLD (Zheng et al. 2021), US case-law sentences"},
    "cuad_audit_rights": {"license": "CC-BY-4.0", "origin": "CUAD (Atticus Project), commercial-contract clauses"},
    "definition_classification": {"license": "CC-BY-SA-4.0", "origin": "Kevin Tobia, US Supreme Court sentences"},
}


def legalbench(raw: Path) -> None:
    md = json.loads((raw / "source" / "task_metadata.json").read_text(encoding="utf-8"))
    rows = []
    tasks_meta = {}
    for task, info in LB_TASKS.items():
        tm = md[task]
        assert sorted(tm["answer_space"]) == ["No", "Yes"], tm["answer_space"]
        counts = {}
        for split in ("train", "test"):
            with (raw / "source" / task / f"{split}.tsv").open(encoding="utf-8", newline="") as f:
                reader = list(csv.DictReader(f, delimiter="\t"))
            counts[split] = len(reader)
            for r in reader:
                meta = {"split": split, "task": task, "index": r["index"]}
                if r.get("document_name"):
                    meta["document_name"] = r["document_name"]
                rows.append({"id": f"{task}-{split}-{r['index']}", "input": r["text"], "label": r["answer"],
                             "meta": meta})
        tasks_meta[task] = {
            **info,
            "question_template": tm["instruction"],
            "answer_space": tm["answer_space"],
            "eval_method": tm.get("eval_method"),
            "rows": counts,
            "label_distribution_test": dict(collections.Counter(
                r["label"] for r in rows if r["meta"]["task"] == task and r["meta"]["split"] == "test")),
            "files": [f"source/{task}/train.tsv", f"source/{task}/test.tsv"],
            "task_readme": f"source/{task}/task_README.md",
        }
    write_outputs(
        raw,
        rows,
        ["Yes", "No"],
        {
            "name": "LegalBench (3 binary tasks)",
            "task": "binary legal text classification; one shared file, select a task with meta.task",
            "source_url": "https://huggingface.co/datasets/nguha/legalbench",
            "source_revision": "daec8237410aa23e3faf4bc41ad8b3a7e1696826",
            "task_docs": "https://github.com/HazyResearch/legalbench/tree/main/tasks",
            "license": "Per task (repo card says cc-by-4.0 but notes tasks carry different licenses): overruling "
            "CC-BY-4.0, cuad_audit_rights CC-BY-4.0, definition_classification CC-BY-SA-4.0",
            "selection": "Binary (Yes/No) tasks with >= 300 test rows and permissive licenses, from three different "
            "upstream sources. hearsay (94 test rows) and contract_nli_* (each < 300 test rows) were too small. "
            "Larger alternatives such as privacy_policy_qa, learned_hands_*, opp115_* and consumer_contracts_qa are "
            "non-commercial (CC-BY-NC*).",
            "tasks": tasks_meta,
            "field_mapping": {
                "id": "'<task>-<split>-<index>'",
                "input": "text (the sentence/clause only; the per-task question lives in tasks.<task>.question_template, "
                "where {{text}} is the slot)",
                "label": "answer (Yes | No)",
                "meta": "task, split (train = the few-shot examples LegalBench ships, test = evaluation), index, "
                "document_name (CUAD only)",
            },
            "caveats": [
                "'Yes'/'No' mean different things per task. Always group or filter by meta.task and give the task "
                "question to the judge.",
                "train splits are just 6-8 few-shot examples per task. Use test for evaluation.",
                "definition_classification is CC-BY-SA-4.0: derived datasets you redistribute must be share-alike.",
                "cuad_audit_rights is balanced by construction (negatives are random other CUAD clauses), so it is "
                "easier than real clause review.",
                "LegalBench is public (2023), so contamination is possible. Cite LegalBench (Guha et al. 2023) plus each "
                "task's original source when publishing.",
            ],
        },
    )


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    name, raw = sys.argv[1], Path(sys.argv[2]).resolve()
    fn = {
        "jev_phishing_bench": jev_phishing_bench,
        "banking77": banking77,
        "civil_comments": civil_comments,
        "mt_bench_human_judgments": mt_bench_human_judgments,
        "legalbench": legalbench,
    }[name]
    fn(raw)


if __name__ == "__main__":
    main()
