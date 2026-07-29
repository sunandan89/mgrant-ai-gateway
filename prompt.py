"""Prompt + output schema for the ATG / 80G receipt checker (prompt_version: atg-v1)."""

OUTPUT_SCHEMA = {
    "document_type": "atg_80g_receipt",
    "origin": "system_generated | handwritten | scanned_typed",
    "extraction_confidence": 0.0,
    "fields": {
        "receipt_no": None, "receipt_date": None, "donor_name": None,
        "donor_pan": None, "amount_figures": None, "amount_words": None,
        "payment_date": None, "txn_ref": None, "purpose": None,
        "project_id": None, "ngo_name": None, "ngo_pan": None,
        "reg_80g_urn": None, "reg_80g_valid_to_ay": None, "signatory": None,
    },
    "authenticity": {"signature_present": False, "stamp_present": False},
    "checks": [{"id": "string", "status": "pass|warn|fail", "detail": "string"}],
    "verdict": "valid | review | blocked",
    "gating": {"disbursement_ok": True, "reason": "string"},
    "flags_count": 0,
}

CHECKS = """
doc_type_ok, fields_complete, amount_figures_words_match, amount_matches_disbursement,
donor_identity_match, txn_ref_present, receipt_date_valid, project_match,
reg_80g_present, reg_80g_covers_fy, ngo_pan_matches_profile,
origin_detected, signature_present, stamp_present, duplicate_check
"""

SYSTEM = """You validate an Indian 80G / ATG donation receipt that an NGO has uploaded to a CSR grant-management system. You are ADVISORY and never final — a human decides.

You are given the receipt (as text and/or images) and a CONTEXT block with the values already on record in the system (expected amount, funding entity, disbursement date, the donation's financial year, and the NGO PAN).

Do two things:
1. EXTRACT the fields. If a field is genuinely not present, return null — never guess. Detect the document ORIGIN: system_generated (clean digital text), handwritten (hand-filled), or scanned_typed. If not system_generated, lower extraction_confidence. Detect whether a signature and a stamp/seal are present.
2. RUN these checks and return one row each: %s
   Key rules:
   - amount_figures_words_match: the rupee figure must equal the amount in words (flag malformed words).
   - amount_matches_disbursement / donor_identity_match: compare against CONTEXT.expected_amount and CONTEXT.funding_entity. Mismatch on these is a hard fail.
   - reg_80g_covers_fy: the 80G validity (…valid_to_ay) must cover CONTEXT.donation_fy. Remember FY N to N+1 is assessed in AY (N+1) to (N+2). If the certificate's last valid AY is earlier than the donation's assessment year, this is a hard FAIL and gating.disbursement_ok = false.
   - duplicate_check: you cannot see history; return pass unless the document itself says duplicate/copy.

Verdict rollup: "blocked" if any of {reg_80g_covers_fy, amount_matches_disbursement, donor_identity_match, duplicate_check} failed; else "review" if any warn/fail; else "valid". Set gating.disbursement_ok=false only when blocked on 80G-window / amount / donor.

Return ONLY a single JSON object with exactly these keys: document_type, origin, extraction_confidence, fields, authenticity, checks, verdict, gating, flags_count. No prose, no markdown fences.
""" % CHECKS

def user_context(context: dict) -> str:
    import json
    return "CONTEXT (values on record):\n" + json.dumps(context, ensure_ascii=False, indent=2)
