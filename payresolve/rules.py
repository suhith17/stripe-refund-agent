"""Narrow, documented regex baseline. No labels, fixture state, or model access."""

import re

from .intent import Intent, PAYMENT_ID

RULES_VERSION = "refund-rules-v1"
ACTION = re.compile(r"\b(?:refund|return|reimburse)\b|\bback\b", re.I)
NUMBER = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
MONEY = re.compile(
    rf"(?<![\w.])(?:(?P<prefix>USD|EUR|GBP|\$|€|£)\s*)?"
    rf"(?P<number>{NUMBER})(?![\w,]|\.\d)(?:\s*(?P<suffix>USD|EUR|GBP|(?:US )?dollars?))?", re.I)


def extract(message):
    ids = list(dict.fromkeys(PAYMENT_ID.findall(message)))
    negative = re.search(r"\b(?:do not|don't|never)\s+(?:refund|return|reimburse)\b|\bno refund\b", message, re.I)
    clauses = [part for part in re.split(r"[;!?]|\.(?!\d)\s+", message) if ACTION.search(part)]
    request = " ".join(clauses)
    action = "other" if negative or not clauses else "refund"
    full = bool(re.search(r"\b(?:full|entire|all(?: of)?(?: the)? remaining)\b", request, re.I))
    amounts = list(MONEY.finditer(request))
    ambiguous = action == "refund" and (len(ids) > 1 or len(amounts) > 1 or bool(
        re.search(r"\b(?:if|unless|either|maybe)\b", request, re.I)))
    amount = evidence = currency = None
    kind = "unspecified"
    if action == "refund":
        if full:
            kind = "full"
        elif amounts:
            kind = "partial"
        if len(amounts) == 1:
            match = amounts[0]
            evidence = match.group(0)
            raw = match.group("number").replace(",", "")
            if re.fullmatch(r"-?\d{1,8}(?:\.\d{1,2})?", raw) and not re.search(r"%|\bcents?\b|\bpercent\b", request, re.I):
                amount = raw
            marker = (match.group("prefix") or match.group("suffix") or "").lower()
            currency = ({"€": "eur", "£": "gbp", "$": "usd"}.get(marker)
                        or (marker if marker in ("usd", "eur", "gbp") else "usd" if "dollar" in marker else None))
        elif full:
            marker = re.search(r"\b(USD|EUR|GBP)\b|\$", request, re.I)
            if marker:
                currency = "usd" if marker.group() == "$" else marker.group().lower()
    return Intent(intent=action, payment_ids=ids, order_id=None, refund_type=kind,
                  amount=amount, amount_evidence=evidence, currency=currency,
                  ambiguous=ambiguous, reason=None)
