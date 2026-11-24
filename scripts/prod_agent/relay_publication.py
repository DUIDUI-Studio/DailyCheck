"""The ordinary typed public outcome and its credential-free relay projection."""
from dataclasses import asdict

from scripts.blue_green.deploy_report import PublicDeployFacts, _payload_digest, render_public
from scripts.blue_green.publication_authority import _validate_outcome, require_public_deployment
from scripts.prod_agent.relay_protocol import digest, require


def freeze(outcome):
    success = outcome["result"] == "success"
    stage = {"preflight": "aborted", "finalize-denied": "merge"}.get(
        outcome["stage"], outcome["stage"])
    facts = PublicDeployFacts(
        outcome["pr"], outcome["sha"], outcome["public_receipt"], outcome["outcome_at"],
        outcome["rc"], stage, "success" if success else "unknown", "undetermined",
        "unverifiable", "none" if success else "audit")
    outcome["relay_publication"] = True
    outcome["facts"] = asdict(facts)
    outcome["payload_sha256"] = _payload_digest(render_public(facts))


def projection(state, expected, row):
    require(state["generation"] == expected["generation"]
            and row["phase"] in {"settled", "rolled_back"}, "no current reportable outcome")
    outcome = _validate_outcome(expected["attempt"], state["outcomes"].get(expected["attempt"]),
                                expected["generation"])
    require_public_deployment(outcome, expected["pr"])
    require(outcome["pr"] == expected["pr"] and outcome["sha"] == expected["head"]
            and outcome["relay_publication"] is True and outcome["public_receipt"] == row["receipt"]
            and (outcome["result"] == "success") == (row["phase"] == "settled"),
            "report outcome identity changed")
    facts = PublicDeployFacts(**outcome["facts"])
    checksum = _payload_digest(render_public(facts))
    require(checksum == outcome["payload_sha256"] and facts.rc == outcome["rc"]
            and facts.receipt == outcome["public_receipt"] and facts.outcome_at == outcome["outcome_at"]
            and facts.pr == expected["pr"] and facts.sha == expected["head"], "report body changed")
    bound = digest({"target": expected, "payload": checksum, "receipt": facts.receipt})
    saved = state["publications"].get(expected["attempt"])
    if saved is None:
        return {"state": "ready", "outcome": bound, "facts": outcome["facts"], "comment_id": 0}
    require(saved["generation"] == expected["generation"] and saved["binding_sha256"] == expected["source"]
            and saved["payload_sha256"] == checksum and saved["relay_outcome"] == bound
            and saved["state"] in {"uncertain", "linked"}, "publication ledger changed")
    return {"state": saved["state"], "outcome": bound, "facts": outcome["facts"],
            "comment_id": saved["comment_id"], "intent": saved["relay_intent"]}
