"""A decision commits only the source revision that produced it."""

from llm import decide
from storage import apply_decision, get_post


def process_post(post_id: int) -> str | None:
    post = get_post(post_id)
    if not post:
        return None
    revision = post["revision"]
    decision = decide(post_id)
    decided_revision = decision.audit.get("revision", revision)
    return apply_decision(
        post_id, decision.model_dump(), expected_revision=decided_revision
    )
