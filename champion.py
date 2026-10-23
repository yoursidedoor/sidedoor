"""Bind AI advocate summaries to the existing inbound reply and send claim."""

import conversion
import champion_composition
import pipeline_routing as routing
import reply_resume


def describe(database, config, action, request, handoff, now):
    decision = handoff["classification"]
    proposal = decision.get("champion_handoff")
    if proposal is None:
        return None
    if config.get("conversion", {}).get("champion_handoff_enabled") is not True:
        raise ValueError("CHAMPION_HANDOFF_DISABLED")
    if (decision["label"] != "AGREEMENT" or handoff.get("can_advance_conversion") is not True
            or decision["unknown_factual_requirements"] or not decision.get("reply_proposal")):
        raise ValueError("CHAMPION_VERIFIED_AGREEMENT_REQUIRED")
    stamp = conversion.timestamp(now)
    # A blank review date is allowed (owner, 2026-09-24); only a dated one can be already due.
    deadline = conversion.timestamp(decision["review_at"]) if decision["review_at"] else None
    if deadline is not None and deadline <= stamp:
        raise ValueError("CHAMPION_REVIEW_DUE_REASSESS_BEFORE_SEND")
    job_key = proposal["job_key"]
    matches = [item for item in request["job_scope"] if item["job_key"] == job_key]
    if len(matches) != 1 or decision["role_reference"] != job_key:
        raise ValueError("CHAMPION_EXACT_ROLE_REQUIRED")
    scope = matches[0]
    job = conversion._job(database, job_key)
    if job["state"] in {"INELIGIBLE", "CLOSED"} or database.execute(
            "SELECT 1 FROM conversion_outcomes WHERE job_key=? AND kind IN ('REJECTED','WITHDRAWN')",
            (job_key,)).fetchone():
        raise ValueError("CHAMPION_ROLE_TERMINAL")
    identity = request["source_snapshot"]["identity"]
    # The agreement is the person's referral record (owner, 2026-09-25: referrals are tracked on
    # the person, independently of jobs). This required an applied per-job REFERRAL_AGREED event,
    # a matching `conversion_referrals` row and no conflicting per-job events. The summary may
    # still describe a role - that is its content - but the referral is not tied to it.
    import referrals
    referral = referrals.current(database, identity)
    if (not referral or referral["state"] != "AGREED" or referral["source_key"] != handoff["source_key"]
            or referral["history"][-1]["evidence"].get("decision_provenance", {}).get("task_id") != action["reply_task_id"]
            or referral["history"][-1]["evidence"].get("decision_provenance", {}).get("output_sha256") != handoff["output_sha256"]):
        raise ValueError("CHAMPION_PERSON_REFERRAL_AGREEMENT_REQUIRED")
    agreement_id = "referral:" + identity + ":" + handoff["source_key"]
    role_context = scope.get("role_context") or {}
    role = routing.one(database, "SELECT evidence_id FROM conversion_roles WHERE job_key=?", (job_key,))
    if not role or role["evidence_id"] != role_context.get("evidence_id"):
        raise ValueError("CHAMPION_CURRENT_ROLE_EVIDENCE_REQUIRED")
    role_evidence = conversion._evidence(database, role["evidence_id"], "ROLE_CONTEXT", stamp)
    if role_evidence["facts"] != role_context.get("facts"):
        raise ValueError("CHAMPION_ROLE_CONTEXT_CHANGED")
    if scope.get("rw_result_id") is None:
        # A job on file at the conversation's company (owner, 2026-09-30: the role-scope bug);
        # it must still be one of that company's open jobs today.
        if job_key not in champion_composition.company_jobs(database, action["profile"]):
            raise ValueError("CHAMPION_FRESH_ROLE_CONTACT_BINDING_REQUIRED")
    else:
        bindings = routing._bindings(database, routing.rows(database, "SELECT * FROM rw_results"), action["profile"], stamp)
        if not any(binding["job"]["job_key"] == job_key and binding["rw_result_id"] == scope["rw_result_id"]
                   and binding["role_proofs"] == role_evidence["facts"].get("source_proofs") for binding in bindings):
            raise ValueError("CHAMPION_FRESH_ROLE_CONTACT_BINDING_REQUIRED")
    if (request.get("application_referral_independent") is not True
            and scope.get("application_interview_context") != champion_composition.history_context(database, job)
            or scope.get("candidate_profile") != champion_composition.candidate_profile(config)):
        raise ValueError("CHAMPION_CURRENT_CANDIDATE_OR_PIPELINE_CONTEXT_CHANGED")
    resume = request["candidate_resume"]
    if resume["source_sha256"] != reply_resume.sha256():
        raise ValueError("CHAMPION_DESIGNATED_RESUME_REQUIRED")
    provenance = {"reply_task_id": action["reply_task_id"], "action_id": action["action_id"],
                  "agreement": agreement_id, "role": role["evidence_id"],
                  "candidate_resume": {key: value for key, value in resume.items() if key != "text"},
                  "resume_quotes": proposal["resume_quotes"], "role_quotes": proposal["role_quotes"],
                  "input_sha256": handoff["input_sha256"], "output_sha256": handoff["output_sha256"],
                  "deadline": deadline, "next_action": decision["next_action"],
                  "job_scope": scope, "source_key": handoff["source_key"]}
    summary_id = "champion:" + routing.digest([job_key, identity, action["message"], provenance])
    return {"summary_id": summary_id, "job_key": job_key, "contact_id": identity,
            "body": action["message"], "provenance": provenance}


def attach(database, config, action, request, handoff, now):
    summary = describe(database, config, action, request, handoff, now)
    if summary is not None:
        action.update(champion_summary_id=summary["summary_id"], champion_job_key=summary["job_key"],
                      champion_deadline=summary["provenance"]["deadline"],
                      champion_next_action=summary["provenance"]["next_action"])
        action["company"] = conversion._job(database, summary["job_key"])["employer"]
    return action


def persist(database, config, action, request, handoff, now):
    summary = describe(database, config, action, request, handoff, now)
    if summary is None:
        return
    previous = routing.one(database, "SELECT * FROM conversion_summaries WHERE summary_id=?", (summary["summary_id"],))
    if previous:
        if previous["body"] != summary["body"] or previous["provenance"] != conversion.encoded(summary["provenance"]):
            raise ValueError("CHAMPION_IMMUTABLE_SUMMARY_CONFLICT")
        return
    version = database.execute("SELECT COALESCE(MAX(version),0)+1 FROM conversion_summaries WHERE job_key=? AND contact_id=?",
                               (summary["job_key"], summary["contact_id"])).fetchone()[0]
    database.execute("INSERT INTO conversion_summaries VALUES (?,?,?,?,?,?,?)",
                     (summary["summary_id"], summary["job_key"], summary["contact_id"], version,
                      summary["body"], conversion.encoded(summary["provenance"]), conversion.timestamp(now)))
    job = conversion._job(database, summary["job_key"])
    conversion._task(database, summary["summary_id"], job, "GUARDED_CHAMPION_DELIVERY",
        {"summary_id": summary["summary_id"], "version": version, "contact_id": summary["contact_id"],
         "action_id": action["action_id"], "exact_message": summary["body"], "delivery_state": "NOT_SENT",
         "next_action": summary["provenance"]["next_action"], "deadline": summary["provenance"]["deadline"],
         "send_path": "guarded_dispatcher", "provenance": summary["provenance"]},
        summary["provenance"]["deadline"], owner="guarded_dispatcher")
