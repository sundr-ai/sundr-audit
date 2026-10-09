"""A synthetic phishing task for demos and tests, answered by simulated backends."""

from __future__ import annotations

import numpy as np

from .backends.simulated import SimulatedBackend
from .compiler import TaskSpec, verdict_questions
from .types import Question, Record

TASK = TaskSpec(
    name="phishing-triage",
    description="Decide whether this email is a phishing attempt or a legitimate message.",
    labels=["phishing", "legitimate"],
    label_descriptions={
        "phishing": "tries to steal credentials, money or data by impersonation",
        "legitimate": "an ordinary message from a real sender",
    },
)

SIGNALS = [
    ("sig_domain_mismatch", "The sender's display name and email domain belong to different organisations."),
    ("sig_free_hosting", "The email links to a free web-hosting or URL-shortening service."),
    ("sig_urgency", "The email pressures the reader to act immediately."),
    ("sig_lure", "The email offers a prize, refund or reward."),
    ("sig_credentials", "The email asks the reader to sign in or confirm account details."),
    ("sig_generic_greeting", "The email greets the reader generically instead of by name."),
    ("sig_known_vendor", "The email comes from a vendor the recipient already does business with."),
    ("sig_attachment_lure", "The email urges the reader to open an attachment."),
]


def questions() -> list[Question]:
    return verdict_questions(TASK) + [Question(name=n, type="noul", instructions=t) for n, t in SIGNALS]


def records(n: int = 20_000, seed: int = 0, phishing_share: float = 0.3) -> list[Record]:
    rng = np.random.default_rng(seed)
    labels = np.where(rng.random(n) < phishing_share, "phishing", "legitimate")
    return [
        Record(id=f"demo-{i}", input=f"Synthetic email #{i}", reference=str(label), cost_usd=0.004)
        for i, label in enumerate(labels)
    ]


def backends(recs: list[Record]) -> list[SimulatedBackend]:
    truth = {r.input: r.reference for r in recs}
    return [
        SimulatedBackend(truth, TASK.labels, name="jev-sim", verdict_strength=1.6, signal_strength=1.2),
        SimulatedBackend(truth, TASK.labels, name="clef-sim", verdict_strength=1.8, signal_strength=1.1),
    ]
