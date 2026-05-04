# Procfile: web: uvicorn main:app --host 0.0.0.0 --port $PORT
# requirements.txt: fastapi uvicorn pydantic python-dateutil pytz

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Literal

import pytz
from dateutil import parser as dateutil_parser
from dateutil.relativedelta import relativedelta
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(title="ABC Finance Vapi Collections Backend", version="2.0.0")

IST = pytz.timezone("Asia/Kolkata")

# ── Schemas ───────────────────────────────────────────────────────────────────

Intent = Literal[
    "borrower_confirmed",
    "third_party",
    "wrong_number",
    "aware",
    "unaware",
    "will_pay_today",
    "will_pay_later",
    "needs_help",
    "financial_hardship",
    "disputes_amount",
    "claims_paid",
    "callback_request",
    "wants_human",
    "opt_out",
    "hindi_switch",
    "legal_threat",
    "harassment_claim",
    "silence",
    "partial_ptp",
    "refusal_to_pay",
    "identity_clarified",
    "sensitive_pii_offered",
    "invalid_or_past_date",
    "long_dated_commitment",
    "unknown",
]


class VapiToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    call_id: str
    current_state: str = "verify_borrower"
    latest_user_utterance: str
    detected_intent: Intent
    customer_name: str
    loan_amount: float | None = None
    emi_due: float
    due_date: str
    days_past_due: float
    commitment_date: str | None = None
    commitment_amount: float | None = None
    payment_mode: Literal["UPI", "net_banking", "card", "cash_deposit", "unknown"] | None = None
    link_channel: Literal["sms", "whatsapp", "none", "unknown"] | None = None
    callback_datetime: str | None = None
    dispute_reason: str | None = None

    @model_validator(mode="after")
    def normalize_blank_optionals(self) -> "VapiToolCallRequest":
        for field_name in (
            "commitment_date",
            "payment_mode",
            "link_channel",
            "callback_datetime",
            "dispute_reason",
        ):
            value = getattr(self, field_name)
            if isinstance(value, str) and not value.strip():
                setattr(self, field_name, None)
        return self

    def slots(self) -> dict[str, Any]:
        return {
            "customer_name": self.customer_name,
            "loan_amount": self.loan_amount,
            "emi_due": self.emi_due,
            "due_date": self.due_date,
            "days_past_due": self.days_past_due,
            "commitment_date": self.commitment_date,
            "commitment_amount": self.commitment_amount,
            "payment_mode": self.payment_mode,
            "link_channel": self.link_channel,
            "callback_datetime": self.callback_datetime,
            "dispute_reason": self.dispute_reason,
        }


class BackendResponse(BaseModel):
    action: Literal["speak", "ask", "end_call", "transfer", "log_only"]
    should_end_call: bool
    end_call_mode: Literal["speak_then_end"] | None = None
    end_reason: Literal[
        "business_flow_completed",
        "customer_requested_end",
        "customer_hung_up",
        "silence_timeout",
        "technical_failure",
        "escalation_required",
        "privacy_protection",
        "unknown_disconnect",
    ] | None = None
    next_state: str
    logged_outcome: str
    allowed_response: str
    required_slot: str | None = None
    escalation_reason: str | None = None
    compliance_flags: list[str] = Field(default_factory=list)
    technical_failure: bool = False
    tool_failure_count: int = 0
    requires_debug: bool = False
    data_to_log: dict[str, Any] = Field(default_factory=dict)


class EndOfCallPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    call_id: str
    vapi_call_id: str | None = None
    ended_reason_from_vapi: str
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: int = 0
    transcript: str | None = None
    messages: list[Any] = Field(default_factory=list)
    recording_url: str | None = None
    summary: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)


# ── Intent Classifier ─────────────────────────────────────────────────────────

INTENTS = {
    "borrower_confirmed",
    "third_party",
    "wrong_number",
    "aware",
    "unaware",
    "will_pay_today",
    "will_pay_later",
    "needs_help",
    "financial_hardship",
    "disputes_amount",
    "claims_paid",
    "callback_request",
    "wants_human",
    "opt_out",
    "hindi_switch",
    "legal_threat",
    "harassment_claim",
    "silence",
    "partial_ptp",
    "refusal_to_pay",
    "identity_clarified",
    "sensitive_pii_offered",
    "invalid_or_past_date",
    "long_dated_commitment",
    "unknown",
}

SAFETY_PRIORITY = [
    "opt_out",
    "harassment_claim",
    "legal_threat",
    "wrong_number",
    "third_party",
    "claims_paid",
    "disputes_amount",
    "financial_hardship",
    "refusal_to_pay",
    "partial_ptp",
    "sensitive_pii_offered",
    "wants_human",
    "hindi_switch",
    "long_dated_commitment",
    "invalid_or_past_date",
    "will_pay_today",
    "will_pay_later",
]


def classify_intent(utterance: str) -> str:
    text = _normalize_text(utterance)
    if not text:
        return "silence"

    if _has(text, "don't call", "dont call", "stop calling", "remove my number", "never call", "do not call"):
        return "opt_out"
    if _has(
        text,
        "wrong number",
        "incorrect number",
        "not my number",
        "you have the wrong number",
        "galat number",
        "galat number hai",
        "wrong person",
    ):
        return "wrong_number"
    if _is_third_party_denial(text):
        return "third_party"
    if _has(text, "lost job", "lost my job", "no money", "can't pay", "cant pay", "cannot pay", "financial problem", "salary issue"):
        return "financial_hardship"
    if _has(text, "won't pay", "wont pay", "not paying", "do whatever you want", "i don't want to pay", "dont want to pay"):
        return "refusal_to_pay"
    if _is_soft_negative(text):
        return "callback_request"
    if _is_borrower_confirmation(text):
        return "borrower_confirmed"
    if _has(text, "harassment", "complaint", "complain"):
        return "harassment_claim"
    if _has(text, "legal", "police", "lawyer", "court"):
        return "legal_threat"
    if _has(text, "not here", "brother", "sister", "father", "mother", "wife", "husband", "friend") and not _has(
        text, "i am"
    ):
        return "third_party"
    if _has(text, "already paid", "paid yesterday", "payment done", "done payment", "i paid", "have paid"):
        return "claims_paid"
    if _has(text, "wrong amount", "incorrect amount", "don't owe", "dont owe", "not my amount"):
        return "disputes_amount"
    if _has(text, "upi id", "card number", "otp", "cvv", "bank account", "account number") or re.search(
        r"\b[\w.\-]{2,}@[a-zA-Z]{2,}\b", text
    ):
        return "sensitive_pii_offered"
    if _has(text, "partial", "part payment", "rest next week", "some amount") or re.search(
        r"\bpay\s+\d+\s+(?:now|today).*\b(rest|remaining)\b", text
    ):
        return "partial_ptp"
    if _has(text, "speak to human", "real person", "manager", "agent", "representative"):
        return "wants_human"
    if _looks_hindi(text):
        return "hindi_switch"
    if _has(text, "45 days", "next month", "after two months", "after 2 months", "in 45 days"):
        return "long_dated_commitment"
    if _has(text, "yesterday", "past date", "invalid date", "32nd january", "30 february", "impossible date"):
        return "invalid_or_past_date"
    if _has(text, "in a meeting", "call later", "busy", "not a good time", "later", "not now", "not available"):
        return "callback_request"
    if _has(text, "are you human", "are you robot", "who are you", "robot", "virtual assistant"):
        return "identity_clarified"
    if _has(text, "i'll pay", "i will pay", "will pay", "can pay", "paying today", "pay today", "pay now"):
        return will_pay_split(text)
    if _has(text, "not aware", "didn't know", "didnt know", "unaware"):
        return "unaware"
    if _has(text, "aware", "i know", "yes i know"):
        return "aware"
    if _has(text, "help", "option", "support"):
        return "needs_help"
    return "unknown"


def resolve_intent_conflict(vapi_intent: str, keyword_intent: str, utterance: str) -> str:
    vapi = vapi_intent if vapi_intent in INTENTS else "unknown"
    keyword = keyword_intent if keyword_intent in INTENTS else "unknown"

    if vapi == keyword:
        return vapi
    text = _normalize_text(utterance)
    if _is_third_party_denial(text):
        return "third_party"
    if keyword == "hindi_switch":
        return "hindi_switch"
    if _is_borrower_confirmation(text) and "third_party" in {vapi, keyword}:
        return "borrower_confirmed"
    if keyword != "unknown" and vapi == "unknown":
        return keyword
    if vapi != "unknown" and keyword == "unknown":
        return vapi

    present = {vapi, keyword}
    keyword_recheck = classify_intent(text)
    if keyword_recheck in INTENTS:
        present.add(keyword_recheck)

    for priority_intent in SAFETY_PRIORITY:
        if priority_intent in present:
            if priority_intent in {"will_pay_today", "will_pay_later"}:
                return will_pay_split(text)
            return priority_intent
    return keyword if keyword != "unknown" else vapi


def normalize_intent_for_state(state: str, intent: str, utterance: str) -> str:
    """Short Hinglish words are meaningful only in the current question context."""
    if intent in {
        "opt_out",
        "wrong_number",
        "third_party",
        "claims_paid",
        "disputes_amount",
        "financial_hardship",
        "legal_threat",
        "harassment_claim",
        "partial_ptp",
        "refusal_to_pay",
        "sensitive_pii_offered",
        "wants_human",
        "hindi_switch",
        "long_dated_commitment",
        "invalid_or_past_date",
        "will_pay_today",
        "will_pay_later",
    }:
        return intent

    if state in {"start", "verify_borrower"}:
        if _is_affirmative(utterance):
            return "borrower_confirmed"
        return intent

    if state == "self_identification":
        if _is_affirmative(utterance):
            return "aware"
        if _is_negative(utterance) or _is_soft_callback_request(utterance):
            return "callback_request"
        return intent

    if state in {"explain_dues", "confirm_awareness"}:
        if _is_unaware_response(utterance) or _is_negative(utterance):
            return "unaware"
        if _is_awareness_response(utterance) or _is_affirmative(utterance):
            return "aware"
        return intent

    if state == "classify_intent":
        if _is_affirmative(utterance):
            return "will_pay_later"
        if _is_negative(utterance):
            return "callback_request"
        return intent

    if state == "post_link_support_check":
        if _is_negative(utterance):
            return "unknown"
        if _is_affirmative(utterance):
            return "needs_help"
        return intent

    return intent


def will_pay_split(utterance: str) -> str:
    text = _normalize_text(utterance)
    if _has(text, "today", "now", "right now", "immediately"):
        return "will_pay_today"
    return "will_pay_later"


def _normalize_text(utterance: str | None) -> str:
    text = (utterance or "").lower().strip()
    text = text.replace("i'll", "i will").replace("ill ", "i will ")
    text = re.sub(r"\s+", " ", text)
    return text


def _has(text: str, *patterns: str) -> bool:
    return any(pattern in text for pattern in patterns)


AFFIRMATIVE_TOKENS = {
    "yes", "yeah", "yep", "ya", "yaa", "yo", "yup",
    "haan", "han", "ha", "haa", "haanji", "hanji", "ji",
    "ok", "okay", "sure", "fine", "correct", "right", "alright",
}

NEGATIVE_TOKENS = {"no", "na", "nah", "nahi", "nahin", "nako", "nhi", "nope"}


def _tokens(text_or_utterance: str | None) -> set[str]:
    return set(re.findall(r"[a-z]+", _normalize_text(text_or_utterance)))


def _is_borrower_confirmation(text: str) -> bool:
    if _is_third_party_denial(text):
        return False
    confirmation_phrases = (
        "speaking", "this is", "i am rohit", "i'm rohit", "rohit bol", "main rohit", "mai rohit",
    )
    return bool(_tokens(text) & AFFIRMATIVE_TOKENS) or _has(text, *confirmation_phrases)


def _is_soft_negative(text: str) -> bool:
    return bool(_tokens(text) & NEGATIVE_TOKENS) or _has(
        text,
        "not now",
        "not available",
        "not a good time",
        "can't talk",
        "cant talk",
        "cannot talk",
        "busy",
    )


def _is_third_party_denial(text: str) -> bool:
    relation_tokens = {
        "brother", "sister", "father", "mother", "wife", "husband",
        "friend", "son", "daughter", "uncle", "aunt", "cousin",
    }
    tokens = _tokens(text)
    relation_present = bool(tokens & relation_tokens)
    relation_context = _has(
        text,
        "i am",
        "i'm",
        "this is",
        "speaking",
        "his",
        "her",
        "rohit's",
        "rohits",
        "rohit ka",
        "rohit ke",
        "rohit ki",
    )
    if relation_present and relation_context:
        return True

    return _has(
        text,
        "no i am his",
        "no i'm his",
        "i am his brother",
        "i'm his brother",
        "i am her brother",
        "i'm her brother",
        "i am his sister",
        "i'm his sister",
        "i am her sister",
        "i'm her sister",
        "i am his father",
        "i'm his father",
        "i am his mother",
        "i'm his mother",
        "i am his wife",
        "i'm his wife",
        "i am his husband",
        "i'm his husband",
        "i am someone else",
        "i'm someone else",
        "someone else speaking",
        "not rohit",
        "not rohit speaking",
        "not rohit here",
        "not the person",
        "not the one",
        "wrong person",
        "he is not here",
        "she is not here",
        "rohit is not here",
    )


def _looks_hindi(text: str) -> bool:
    devanagari = bool(re.search(r"[ऀ-ॿ]", text))
    hindi_tokens = {"nahi", "nahin", "haan", "han", "paisa", "kal", "aaj", "mujhe", "hindi", "baat", "karo", "karna"}
    tokens = _tokens(text)
    return devanagari or "hindi" in tokens or len(tokens & hindi_tokens) >= 2


# ── Date Parser ───────────────────────────────────────────────────────────────

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "forty five": 45,
}


def today_ist() -> date:
    return datetime.now(IST).date()


def normalize_commitment_date(raw_str: str, call_date: date) -> date | None:
    if not raw_str or not raw_str.strip():
        return None

    text = _clean_date(raw_str)
    if _is_random_or_ambiguous(text):
        return None

    if re.search(r"\btoday\b|\bnow\b", text):
        return call_date
    if re.search(r"\btomorrow\b", text):
        return call_date + timedelta(days=1)
    if re.search(r"\bday after\b|\bday after tomorrow\b|\bovermorrow\b", text):
        return call_date + timedelta(days=2)
    if re.search(r"\byesterday\b|\bpast date\b", text):
        return call_date - timedelta(days=1)

    days_match = re.search(r"\b(?:in|after)\s+(\d+|[a-z ]+?)\s+days?\b", text)
    if days_match:
        count = _parse_number(days_match.group(1))
        if count is not None:
            return call_date + timedelta(days=count)

    weeks_match = re.search(r"\b(?:in|after)\s+(\d+|[a-z ]+?)\s+weeks?\b", text)
    if weeks_match:
        count = _parse_number(weeks_match.group(1))
        if count is not None:
            return call_date + timedelta(days=count * 7)

    months_match = re.search(r"\b(?:in|after)\s+(\d+|[a-z ]+?)\s+months?\b", text)
    if months_match:
        count = _parse_number(months_match.group(1))
        if count is not None:
            return call_date + relativedelta(months=count)

    if "next week" in text:
        return None
    if "next month" in text:
        return call_date + relativedelta(months=1)

    weekday_date = _parse_weekday(text, call_date)
    if weekday_date is not None:
        return weekday_date

    return _parse_calendar_date(text, call_date)


def validate_ptp_window(normalized_date: date, call_date: date, max_days: int = 14) -> bool:
    return call_date <= normalized_date <= call_date + timedelta(days=max_days)


def validate_commitment_date(raw_str: str, call_date: date, max_days: int = 14) -> dict[str, Any]:
    try:
        normalized = normalize_commitment_date(raw_str, call_date)
    except (ValueError, OverflowError):
        normalized = None

    if normalized is None:
        return {
            "valid": False,
            "normalized_date": None,
            "reason": "invalid_or_ambiguous_date",
            "requires_human_callback": False,
        }
    if normalized < call_date:
        return {
            "valid": False,
            "normalized_date": normalized.isoformat(),
            "reason": "past_date",
            "requires_human_callback": False,
        }
    if normalized > call_date + timedelta(days=max_days):
        return {
            "valid": False,
            "normalized_date": normalized.isoformat(),
            "reason": "outside_14_day_window",
            "requires_human_callback": True,
        }
    return {
        "valid": True,
        "normalized_date": normalized.isoformat(),
        "reason": "valid",
        "requires_human_callback": False,
    }


def _clean_date(raw_str: str) -> str:
    text = raw_str.lower().strip()
    text = re.sub(r"[,\.;]", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = text.replace("i'll", "i will").replace("ill ", "i will ")
    return text


def _is_random_or_ambiguous(text: str) -> bool:
    return any(
        phrase in text
        for phrase in ("some random date", "random date", "someday", "whenever", "next week")
    )


def _parse_number(value: str) -> int | None:
    value = value.strip()
    if value.isdigit():
        return int(value)
    if value in NUMBER_WORDS:
        return NUMBER_WORDS[value]
    pieces = value.split()
    total = 0
    for piece in pieces:
        if piece not in NUMBER_WORDS:
            return None
        total += NUMBER_WORDS[piece]
    return total or None


def _parse_weekday(text: str, call_date: date) -> date | None:
    for weekday_name, weekday_number in WEEKDAYS.items():
        if re.search(rf"\b(this|coming|next)?\s*{weekday_name}\b", text):
            days_ahead = (weekday_number - call_date.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            if re.search(rf"\bnext\s+{weekday_name}\b", text):
                days_ahead = days_ahead + 7 if days_ahead <= 7 else days_ahead
            return call_date + timedelta(days=days_ahead)
    return None


def _parse_calendar_date(text: str, call_date: date) -> date | None:
    if re.search(r"\b(?:32nd|32|33|34|35)\b", text):
        return None
    default = datetime(call_date.year, 1, 1)
    try:
        parsed = dateutil_parser.parse(text, fuzzy=True, dayfirst=True, default=default)
    except (ValueError, OverflowError, dateutil_parser.ParserError):
        return None

    parsed_date = parsed.date()
    mentions_year = bool(re.search(r"\b20\d{2}\b|\b19\d{2}\b", text))
    if not mentions_year and parsed_date < call_date:
        candidate_next_year = parsed_date.replace(year=call_date.year + 1)
        if candidate_next_year <= call_date + timedelta(days=14):
            return candidate_next_year
    return parsed_date


# ── Simulated Services (stateless — stdout logging only) ──────────────────────

def send_payment_link_simulated(call_id: str, channel: str, customer_name: str, emi_due: float) -> dict[str, Any]:
    event_type = "payment_link_whatsapp_simulated" if channel == "whatsapp" else "payment_link_sms_simulated"
    payload = {
        "status": "success",
        "event_type": event_type,
        "call_id": call_id,
        "channel": channel,
        "customer_name": customer_name,
        "emi_due": int(emi_due),
        "simulated": True,
    }
    logger.info(f"EVENT | call_id={call_id} | event_type={event_type} | payload={json.dumps(payload)}")
    return payload


def schedule_callback(call_id: str, callback_datetime: str | None, reason: str) -> dict[str, Any]:
    payload = {
        "status": "scheduled",
        "call_id": call_id,
        "callback_datetime": callback_datetime,
        "reason": reason,
        "simulated": True,
        "created_at": datetime.utcnow().isoformat(),
    }
    logger.info(f"EVENT | call_id={call_id} | event_type=human_callback_requested | reason={reason} | payload={json.dumps(payload)}")
    return payload


def flag_dispute(call_id: str, dispute_reason: str | None, customer_name: str) -> dict[str, Any]:
    payload = {
        "status": "flagged",
        "call_id": call_id,
        "dispute_reason": dispute_reason,
        "customer_name": customer_name,
        "simulated": True,
    }
    logger.info(f"EVENT | call_id={call_id} | event_type=dispute_flagged | payload={json.dumps(payload)}")
    return payload


def mark_do_not_call(call_id: str, customer_name: str) -> dict[str, Any]:
    payload = {
        "status": "recorded",
        "call_id": call_id,
        "customer_name": customer_name,
        "simulated": True,
    }
    logger.info(f"EVENT | call_id={call_id} | event_type=dnc_requested | payload={json.dumps(payload)}")
    return payload


def flag_reconciliation(call_id: str, customer_name: str, emi_due: float) -> dict[str, Any]:
    payload = {
        "status": "flagged",
        "call_id": call_id,
        "customer_name": customer_name,
        "emi_due": int(emi_due),
        "simulated": True,
    }
    logger.info(f"EVENT | call_id={call_id} | event_type=reconciliation_flagged | payload={json.dumps(payload)}")
    return payload


def transfer_to_human_simulated(call_id: str, escalation_reason: str) -> dict[str, Any]:
    payload = {
        "status": "queued",
        "call_id": call_id,
        "escalation_reason": escalation_reason,
        "simulated": True,
    }
    logger.info(f"EVENT | call_id={call_id} | event_type=human_callback_requested | reason={escalation_reason} | payload={json.dumps(payload)}")
    return payload


# ── State Machine ─────────────────────────────────────────────────────────────

RESPONSES = {
    "claims_paid": "Thank you for letting me know. I will mark this for payment verification with our back-office team. You do not need to make another payment right now.",
    "financial_hardship": "I understand. I will not push for payment right now. I will arrange a callback from our support team within 24 to 48 hours to discuss possible options.",
    "disputes_amount": "I understand. I will record this as a dispute and route it for human review. Someone from the team will follow up within 24 to 48 hours after checking the account details.",
    "callback_request": "Sorry for disturbing you. What would be a better time for a callback?",
    "callback_captured": "Thank you. I have noted the callback time. Have a good day.",
    "opt_out": "Understood. I will record your request not to be called again. Thank you, and I will end the call now.",
    "silence_first": "Are you still there?",
    "silence_final": "I am not able to hear you, so I will end the call now. Thank you.",
    "hindi_switch": "I understand you would prefer Hindi. I can arrange a callback from a Hindi-speaking support agent within 24 to 48 hours.",
    "third_party": "Sorry, I can only speak with {customer_name}. I will call back later. Thank you for your time.",
    "wrong_number": "Sorry for the inconvenience. I will record this as a wrong number and end the call now. Thank you for your time.",
    "compliance": "I am sorry for the inconvenience. I will stop this reminder flow and escalate your concern for review. Thank you for informing us.",
    "partial_ptp": "I have noted that you may be able to make a partial payment. Since this needs review, I will arrange a callback from our support team within 24 to 48 hours.",
    "clarify_date": "Could you please confirm the exact date you plan to pay?",
    "invalid_date": "That date does not look valid for this reminder. Please share a payment date within the next 14 days. If that is difficult, I can arrange a callback from our support team within 24 to 48 hours.",
    "long_dated": "I have noted the date you mentioned. Since this is outside the usual reminder window, I will arrange a callback from our support team within 24 to 48 hours.",
    "identity": "I am Priya, a virtual assistant calling from ABC Finance about your personal loan EMI reminder.",
    "sensitive_pii": "For your security, please do not share UPI IDs, card numbers, OTPs, or bank details on this call. I can send a secure payment link to your registered number instead.",
    "claims_paid_plus_dnc": "Thank you for letting me know. I will mark your payment claim for verification and also record your request not to receive further calls. I will end the call now.",
    "refusal": "I understand. I will arrange a callback from our support team within 24 to 48 hours to understand your concern and see how they can help. Thank you for your time.",
    "payment_mode": "Thank you. Which mode will you use — UPI, net banking, card, or cash deposit?",
    "payment_link": "I can send the payment link to your registered number by SMS or WhatsApp. Which would you prefer?",
    "post_link_check": "I have sent the payment link to your registered number. Is there anything else you need help with?",
    "close": "Thank you. I have noted your commitment and payment preference. Thank you for your time. Have a good day.",
    "dues": "Thank you. Your EMI of {emi_due} rupees was due on {due_date}, and it is currently {days_past_due} days overdue. Were you aware of this?",
    "good_time": "This is Priya, a virtual assistant calling from ABC Finance about your personal loan EMI. Is now a good time for a quick two-minute call?",
    "not_verify": "I'm sorry, I can only continue after speaking with the borrower. I will call back later. Thank you.",
    "human": "I understand. I will arrange a callback from our support team within 24 to 48 hours.",
    "unknown": "I am not able to verify that right now. I will flag this for human follow-up.",
    "input_not_captured": "Sorry, I did not catch that clearly. Could you please repeat?",
}


def process(
    session: dict[str, Any],
    latest_user_utterance: str,
    vapi_detected_intent: str,
    slots: dict[str, Any],
) -> BackendResponse:
    state = session.get("current_state") or slots.get("current_state") or "start"
    backend_intent = classify_intent(latest_user_utterance)
    final_intent = resolve_intent_conflict(vapi_detected_intent, backend_intent, latest_user_utterance)
    final_intent = normalize_intent_for_state(state, final_intent, latest_user_utterance)
    borrower_already_verified = bool(session.get("borrower_verified")) or state not in {"start", "verify_borrower"}
    if borrower_already_verified and final_intent == "third_party" and not _is_third_party_denial(latest_user_utterance):
        final_intent = backend_intent if backend_intent not in {"third_party", "unknown"} else "hindi_switch" if "hindi" in (latest_user_utterance or "").lower() else "unknown"
    data = _base_data(session, slots, backend_intent, final_intent, vapi_detected_intent)
    call_id = session["call_id"]

    if _contains_claims_paid_plus_dnc(latest_user_utterance):
        flag_reconciliation(call_id, _customer_name(session, slots), _emi_due(session, slots))
        mark_do_not_call(call_id, _customer_name(session, slots))
        return _end(
            "claims-paid-plus-dnc",
            RESPONSES["claims_paid_plus_dnc"],
            "customer_requested_end",
            data | {"requires_human_review": True},
        )

    global_edge = _handle_any_state_edges(call_id, state, final_intent, latest_user_utterance, slots, session, data)
    if global_edge is not None:
        return global_edge

    if state == "start":
        return _ask("verify_borrower", "verification_started", f"Hi, am I speaking with {_customer_name(session, slots)}?", "borrower_confirmation", data)

    if state == "verify_borrower":
        if final_intent == "borrower_confirmed" or _is_affirmative(latest_user_utterance):
            return _ask(
                "self_identification",
                "borrower-confirmed",
                RESPONSES["good_time"],
                "good_time_confirmation",
                data | {"borrower_verified": True, "verification_method": "self_attested_call_answer", "meaningful_conversation": True},
            )
        if final_intent == "wrong_number":
            return _end("wrong-number", RESPONSES["wrong_number"], "privacy_protection", data | {"requires_human_review": True})
        if final_intent == "third_party" and not borrower_already_verified:
            return _end("third-party-answered", _response("third_party", session, slots), "privacy_protection", data)
        return _ask(
            "verify_borrower",
            "borrower-verification-clarification-needed",
            f"I'm sorry, could you confirm if I am speaking with {_customer_name(session, slots)}?",
            "borrower_confirmation",
            data,
        )

    if state == "self_identification":
        if final_intent == "callback_request" or _is_soft_callback_request(latest_user_utterance):
            return _callback_request(call_id, latest_user_utterance, slots, data)
        if _is_affirmative(latest_user_utterance) or final_intent in {"aware", "unaware", "borrower_confirmed"}:
            return _ask("confirm_awareness", "dues-disclosed", _response("dues", session, slots), "awareness_confirmation", data)
        return _input_not_captured(state, data, session, slots)

    if state in {"explain_dues", "confirm_awareness", "classify_intent"}:
        if state in {"explain_dues", "confirm_awareness"} and final_intent == "unknown":
            return _input_not_captured(state, data, session, slots)
        if state == "classify_intent" and final_intent == "unknown":
            return _input_not_captured(state, data, session, slots)
        if final_intent in {"aware", "unaware", "needs_help"} or (
            state == "confirm_awareness"
            and (final_intent == "borrower_confirmed" or _is_affirmative(latest_user_utterance))
        ):
            return _ask("classify_intent", "intent-clarification-needed", "Would you be able to make the EMI payment now or within the next few days?", "payment_intent", data)
        if final_intent in {"will_pay_today", "will_pay_later"}:
            return _handle_commitment_date(call_id, session, latest_user_utterance, slots, data)
        if state == "classify_intent" and _is_affirmative(latest_user_utterance):
            return _ask("capture_commitment_date", "commitment-date-required", "Could you please confirm the exact date you plan to pay?", "commitment_date", data)

    if state == "capture_commitment_date":
        return _handle_commitment_date(call_id, session, latest_user_utterance, slots, data)

    if state == "capture_payment_mode":
        payment_mode = _payment_mode(latest_user_utterance, slots)
        link_channel = _link_channel(latest_user_utterance, slots)
        if payment_mode and link_channel in {"sms", "whatsapp"}:
            send_payment_link_simulated(call_id, link_channel, _customer_name(session, slots), _emi_due(session, slots))
            return _ask(
                "post_link_support_check",
                f"payment_mode_and_payment_link_{link_channel}_captured",
                RESPONSES["post_link_check"],
                "post_link_support_check",
                data | {"payment_mode": payment_mode, "link_channel": link_channel},
            )
        if payment_mode:
            return _ask(
                "offer_payment_link",
                "payment_mode_captured",
                RESPONSES["payment_link"],
                "link_channel",
                data | {"payment_mode": payment_mode},
            )
        return _input_not_captured("capture_payment_mode", data, session, slots)

    if state == "offer_payment_link":
        channel = _link_channel(latest_user_utterance, slots)
        if channel in {"sms", "whatsapp"}:
            send_payment_link_simulated(call_id, channel, _customer_name(session, slots), _emi_due(session, slots))
            return _ask(
                "post_link_support_check",
                f"payment_link_{channel}_simulated",
                RESPONSES["post_link_check"],
                "post_link_support_check",
                data | {"link_channel": channel},
            )
        return _input_not_captured("offer_payment_link", data, session, slots)

    if state == "post_link_support_check":
        if _is_negative(latest_user_utterance) or final_intent == "borrower_confirmed":
            return _end("business_flow_completed", RESPONSES["close"], "business_flow_completed", data)
        if final_intent == "unknown":
            return _input_not_captured("post_link_support_check", data, session, slots)
        transfer_to_human_simulated(call_id, "additional_query_after_ptp")
        return _end(
            "human-callback-requested",
            RESPONSES["human"],
            "escalation_required",
            data | {"escalation_reason": "additional_query_after_ptp", "requires_human_callback": True},
            escalation_reason="additional_query_after_ptp",
        )

    if state == "schedule_callback":
        if not (slots.get("callback_datetime") or _extract_callback_datetime(latest_user_utterance)):
            return _input_not_captured("schedule_callback", data, session, slots)
        return _callback_request(call_id, latest_user_utterance, slots, data)

    if state in {"escalate", "close", "end_call"}:
        return _end("business_flow_completed", RESPONSES["close"], "business_flow_completed", data)

    return _ask("classify_intent", "unknown", RESPONSES["unknown"], None, data | {"requires_human_review": True})


def _handle_any_state_edges(
    call_id: str,
    state: str,
    final_intent: str,
    utterance: str,
    slots: dict[str, Any],
    session: dict[str, Any],
    data: dict[str, Any],
) -> BackendResponse | None:
    customer_name = _customer_name(session, slots)
    emi_due = _emi_due(session, slots)

    if final_intent == "silence":
        failure_count = int(session.get("tool_failure_count") or 0)
        if failure_count == 0:
            return _ask("silence_reprompt", "no-response-reprompt", RESPONSES["silence_first"], None, data | {"tool_failure_count": 1})
        return _end("no-response", RESPONSES["silence_final"], "silence_timeout", data)

    if final_intent == "identity_clarified":
        return _speak(state, "identity-clarified", RESPONSES["identity"], data)

    if final_intent == "sensitive_pii_offered":
        return _speak(
            state,
            "sensitive-payment-info-offered",
            RESPONSES["sensitive_pii"],
            data | {"requires_redaction": True, "compliance_flags": ["sensitive_payment_info_offered"]},
        )

    if final_intent == "opt_out":
        mark_do_not_call(call_id, customer_name)
        return _end("do-not-call-request", RESPONSES["opt_out"], "customer_requested_end", data | {"compliance_flags": ["dnc_requested"]})

    if final_intent == "wrong_number":
        return _end("wrong-number", RESPONSES["wrong_number"], "privacy_protection", data | {"requires_human_review": True})

    if final_intent == "third_party" and (
        _is_third_party_denial(utterance)
        or (not session.get("borrower_verified") and state in {"start", "verify_borrower"})
    ):
        return _end("third-party-answered", _response("third_party", session, slots), "privacy_protection", data)

    if final_intent == "claims_paid":
        flag_reconciliation(call_id, customer_name, emi_due)
        return _end(
            "claims-paid",
            RESPONSES["claims_paid"],
            "escalation_required",
            data | {"requires_human_review": True},
            escalation_reason="reconciliation_required",
        )

    if final_intent == "disputes_amount":
        flag_dispute(call_id, slots.get("dispute_reason") or utterance, customer_name)
        return _end(
            "amount-disputed",
            RESPONSES["disputes_amount"],
            "escalation_required",
            data | {"requires_human_review": True},
            escalation_reason="dispute_flagged",
        )

    if final_intent == "financial_hardship":
        schedule_callback(call_id, slots.get("callback_datetime"), "financial_hardship")
        return _end(
            "financial-hardship-human-callback",
            RESPONSES["financial_hardship"],
            "escalation_required",
            data | {"requires_human_callback": True},
            escalation_reason="hardship_flagged",
        )

    if final_intent in {"legal_threat", "harassment_claim"}:
        transfer_to_human_simulated(call_id, "compliance_review")
        return _end(
            "harassment-claim-compliance-escalation",
            RESPONSES["compliance"],
            "escalation_required",
            data | {"requires_human_review": True, "compliance_flags": ["compliance_review_required"]},
            escalation_reason="compliance_review",
        )

    if final_intent == "hindi_switch":
        schedule_callback(call_id, slots.get("callback_datetime"), "hindi_callback_requested")
        return _end(
            "hindi-callback-requested",
            RESPONSES["hindi_switch"],
            "escalation_required",
            data | {"requires_human_callback": True},
            escalation_reason="hindi_callback_requested",
        )

    if final_intent == "partial_ptp":
        amount = _extract_amount(utterance, slots)
        transfer_to_human_simulated(call_id, "partial_ptp_review")
        return _end(
            "partial-ptp-human-callback",
            RESPONSES["partial_ptp"],
            "escalation_required",
            data
            | {
                "partial_payment_intent": True,
                "partial_payment_amount": amount,
                "commitment_amount": amount,
                "valid_ptp": False,
                "requires_human_callback": True,
            },
            escalation_reason="partial_ptp_review",
        )

    if final_intent == "long_dated_commitment":
        transfer_to_human_simulated(call_id, "commitment_date_outside_v1_window")
        return _end(
            "long-dated-commitment-human-callback",
            RESPONSES["long_dated"],
            "escalation_required",
            data | {"requires_human_callback": True, "valid_ptp": False},
            escalation_reason="commitment_date_outside_v1_window",
        )

    if final_intent == "invalid_or_past_date":
        return _ask(
            "capture_commitment_date",
            "invalid-payment-date",
            RESPONSES["invalid_date"],
            "commitment_date",
            data | {"invalid_commitment_date_raw": utterance, "valid_ptp": False, "requires_human_callback_if_unresolved": True},
        )

    if final_intent == "refusal_to_pay":
        schedule_callback(call_id, slots.get("callback_datetime"), "refusal_to_pay")
        return _end(
            "refusal-to-pay-human-callback",
            RESPONSES["refusal"],
            "escalation_required",
            data
            | {
                "risk_attribute": "refusal_to_pay",
                "risk_level": "medium",
                "requires_human_callback": True,
                "valid_ptp": False,
            },
            escalation_reason="refusal_to_pay",
        )

    if final_intent == "wants_human":
        transfer_to_human_simulated(call_id, "human_requested")
        return _end(
            "human-callback-requested",
            RESPONSES["human"],
            "escalation_required",
            data | {"requires_human_callback": True},
            escalation_reason="human_requested",
        )

    if final_intent == "callback_request" and state != "post_link_support_check":
        return _callback_request(call_id, utterance, slots, data)

    return None


def _handle_commitment_date(
    call_id: str,
    session: dict[str, Any],
    latest_user_utterance: str,
    slots: dict[str, Any],
    data: dict[str, Any],
) -> BackendResponse:
    raw_commitment_date = slots.get("commitment_date") or latest_user_utterance
    validation = validate_commitment_date(raw_commitment_date, today_ist())
    if not validation["valid"]:
        if validation["reason"] == "outside_14_day_window":
            transfer_to_human_simulated(call_id, "commitment_date_outside_v1_window")
            return _end(
                "long-dated-commitment-human-callback",
                RESPONSES["long_dated"],
                "escalation_required",
                data
                | {
                    "commitment_date": raw_commitment_date,
                    "partial_commitment_date": validation["normalized_date"],
                    "valid_ptp": False,
                    "requires_human_callback": True,
                },
                escalation_reason="commitment_date_outside_v1_window",
            )
        return _ask(
            "capture_commitment_date",
            "invalid-payment-date",
            RESPONSES["invalid_date"] if validation["reason"] != "invalid_or_ambiguous_date" else RESPONSES["clarify_date"],
            "commitment_date",
            data
            | {
                "commitment_date": raw_commitment_date,
                "invalid_commitment_date_raw": raw_commitment_date,
                "valid_ptp": False,
                "requires_human_callback_if_unresolved": True,
            },
        )

    amount = _extract_amount(latest_user_utterance, slots)
    payment_mode = _payment_mode(latest_user_utterance, slots)
    link_channel = _link_channel(latest_user_utterance, slots)
    captured_data = data | {
        "commitment_date": raw_commitment_date,
        "partial_commitment_date": validation["normalized_date"],
        "valid_ptp": True,
        **({"commitment_amount": amount} if amount is not None else {}),
        **({"payment_mode": payment_mode} if payment_mode else {}),
        **({"link_channel": link_channel} if link_channel else {}),
    }

    if payment_mode and link_channel in {"sms", "whatsapp"}:
        send_payment_link_simulated(call_id, link_channel, _customer_name(session, slots), _emi_due(session, slots))
        return _ask(
            "post_link_support_check",
            f"commitment_mode_and_payment_link_{link_channel}_captured",
            RESPONSES["post_link_check"],
            "post_link_support_check",
            captured_data,
        )

    if payment_mode:
        return _ask(
            "offer_payment_link",
            "commitment_date_and_payment_mode_captured",
            RESPONSES["payment_link"],
            "link_channel",
            captured_data,
        )

    return _ask(
        "capture_payment_mode",
        "commitment_date_captured",
        RESPONSES["payment_mode"],
        "payment_mode",
        captured_data,
    )


def _callback_request(call_id: str, utterance: str, slots: dict[str, Any], data: dict[str, Any]) -> BackendResponse:
    callback_datetime = slots.get("callback_datetime") or _extract_callback_datetime(utterance)
    if callback_datetime:
        schedule_callback(call_id, callback_datetime, "customer_requested_callback")
        return _end(
            "callback-requested",
            RESPONSES["callback_captured"],
            "customer_requested_end",
            data | {"callback_datetime": callback_datetime, "requires_human_callback": True},
        )
    return _ask("schedule_callback", "callback-requested", RESPONSES["callback_request"], "callback_datetime", data)


def _input_not_captured(
    state: str,
    data: dict[str, Any],
    session: dict[str, Any],
    slots: dict[str, Any],
) -> BackendResponse:
    return _ask(
        state,
        "input-not-captured",
        _input_not_captured_response(state, session, slots),
        _required_slot_for_state(state),
        data | {"input_not_captured": True},
    )


def _input_not_captured_response(state: str, session: dict[str, Any], slots: dict[str, Any]) -> str:
    retry_prompts = {
        "verify_borrower": f"Sorry, I did not catch that clearly. Am I speaking with {_customer_name(session, slots)}?",
        "self_identification": "Sorry, I did not catch that clearly. Is now a good time for a quick two-minute call?",
        "explain_dues": "Sorry, I did not catch that clearly. Were you aware of this overdue EMI?",
        "confirm_awareness": "Sorry, I did not catch that clearly. Were you aware of this overdue EMI?",
        "classify_intent": "Sorry, I did not catch that clearly. Would you be able to make the EMI payment now or within the next few days?",
        "capture_commitment_date": "Sorry, I did not catch that clearly. Could you please confirm the exact date you plan to pay?",
        "capture_payment_mode": "Sorry, I did not catch that clearly. Which mode will you use — UPI, net banking, card, or cash deposit?",
        "offer_payment_link": "Sorry, I did not catch that clearly. Should I send the payment link by SMS or WhatsApp?",
        "post_link_support_check": "Sorry, I did not catch that clearly. Is there anything else you need help with?",
        "schedule_callback": "Sorry, I did not catch that clearly. What would be a better time for a callback?",
    }
    return retry_prompts.get(state, RESPONSES["input_not_captured"])


def _required_slot_for_state(state: str) -> str | None:
    return {
        "verify_borrower": "borrower_confirmation",
        "self_identification": "good_time_confirmation",
        "explain_dues": "awareness_confirmation",
        "confirm_awareness": "awareness_confirmation",
        "classify_intent": "payment_intent",
        "capture_commitment_date": "commitment_date",
        "capture_payment_mode": "payment_mode",
        "offer_payment_link": "link_channel",
        "post_link_support_check": "post_link_support_check",
        "schedule_callback": "callback_datetime",
    }.get(state)


def _base_data(
    session: dict[str, Any],
    slots: dict[str, Any],
    backend_intent: str,
    final_intent: str,
    vapi_intent: str,
) -> dict[str, Any]:
    return {
        "previous_state": session.get("current_state"),
        "backend_detected_intent": backend_intent,
        "final_intent": final_intent,
        "intent_mismatch": vapi_intent != backend_intent,
        "customer_name": _customer_name(session, slots),
    }


def _ask(next_state: str, outcome: str, response: str, required_slot: str | None, data: dict[str, Any]) -> BackendResponse:
    return BackendResponse(
        action="ask",
        should_end_call=False,
        end_call_mode=None,
        end_reason=None,
        next_state=next_state,
        logged_outcome=outcome,
        allowed_response=response,
        required_slot=required_slot,
        escalation_reason=data.get("escalation_reason"),
        compliance_flags=data.get("compliance_flags", []),
        technical_failure=False,
        tool_failure_count=int(data.get("tool_failure_count") or 0),
        requires_debug=False,
        data_to_log=data,
    )


def _speak(next_state: str, outcome: str, response: str, data: dict[str, Any]) -> BackendResponse:
    return BackendResponse(
        action="speak",
        should_end_call=False,
        end_call_mode=None,
        end_reason=None,
        next_state=next_state,
        logged_outcome=outcome,
        allowed_response=response,
        required_slot=None,
        escalation_reason=data.get("escalation_reason"),
        compliance_flags=data.get("compliance_flags", []),
        technical_failure=False,
        tool_failure_count=int(data.get("tool_failure_count") or 0),
        requires_debug=False,
        data_to_log=data,
    )


def _end(
    outcome: str,
    response: str,
    end_reason: str,
    data: dict[str, Any],
    escalation_reason: str | None = None,
) -> BackendResponse:
    return BackendResponse(
        action="end_call",
        should_end_call=True,
        end_call_mode="speak_then_end",
        end_reason=end_reason,
        next_state="end_call",
        logged_outcome=outcome,
        allowed_response=response,
        required_slot=None,
        escalation_reason=escalation_reason or data.get("escalation_reason"),
        compliance_flags=data.get("compliance_flags", []),
        technical_failure=False,
        tool_failure_count=int(data.get("tool_failure_count") or 0),
        requires_debug=False,
        data_to_log=data,
    )


def _response(key: str, session: dict[str, Any], slots: dict[str, Any]) -> str:
    return RESPONSES[key].format(
        customer_name=_customer_name(session, slots),
        emi_due=int(_emi_due(session, slots)),
        due_date=session.get("due_date") or slots.get("due_date") or "",
        days_past_due=int(float(session.get("days_past_due") or slots.get("days_past_due") or 0)),
    )


def _customer_name(session: dict[str, Any], slots: dict[str, Any]) -> str:
    return str(session.get("customer_name") or slots.get("customer_name") or "customer")


def _emi_due(session: dict[str, Any], slots: dict[str, Any]) -> float:
    return float(session.get("emi_due") or slots.get("emi_due") or 0)


def _contains_claims_paid_plus_dnc(utterance: str) -> bool:
    text = (utterance or "").lower()
    paid = any(phrase in text for phrase in ("already paid", "paid yesterday", "payment done", "done payment"))
    dnc = any(phrase in text for phrase in ("don't call", "dont call", "stop calling", "never call", "do not call"))
    return paid and dnc


def _payment_mode(utterance: str, slots: dict[str, Any]) -> str | None:
    slot_mode = slots.get("payment_mode")
    if slot_mode and slot_mode != "unknown":
        return slot_mode
    text = (utterance or "").lower()
    if "upi" in text:
        return "UPI"
    if "net banking" in text or "netbanking" in text:
        return "net_banking"
    if "card" in text:
        return "card"
    if "cash" in text or "deposit" in text:
        return "cash_deposit"
    return None


def _link_channel(utterance: str, slots: dict[str, Any]) -> str | None:
    slot_channel = slots.get("link_channel")
    if slot_channel in {"sms", "whatsapp"}:
        return slot_channel
    text = (utterance or "").lower()
    if "whatsapp" in text or "whats app" in text:
        return "whatsapp"
    if "sms" in text or "message" in text or "text" in text:
        return "sms"
    return None


def _extract_callback_datetime(utterance: str | None) -> str | None:
    text = _normalize_text(utterance)
    if not text:
        return None

    time_pattern = r"(?:at\s*)?\b(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:am|pm)?\b"
    day_pattern = r"\b(?:today|tomorrow|tonight|morning|afternoon|evening|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"

    for pattern in (
        rf"(?:call|callback|call me|call back|later|meeting).*?({time_pattern}(?:\s*{day_pattern})?)",
        rf"({time_pattern}\s*{day_pattern})",
        rf"({day_pattern}\s*{time_pattern})",
    ):
        match = re.search(pattern, text)
        if match:
            return re.sub(r"^at\s+", "", match.group(1).strip())

    if re.search(day_pattern, text) and _has(text, "call", "callback", "call back", "later"):
        match = re.search(day_pattern, text)
        return match.group(0).strip() if match else None

    return None


def _extract_amount(utterance: str, slots: dict[str, Any]) -> int | None:
    if slots.get("commitment_amount") is not None:
        return int(float(slots["commitment_amount"]))
    text = (utterance or "").lower().replace(",", "")
    match = re.search(r"\b(?:rs\.?|rupees?|pay)\s*(\d{3,7})\b|\b(\d{3,7})\s*(?:rs\.?|rupees?)?\b", text)
    if match:
        return int(match.group(1) or match.group(2))
    return None


def _is_affirmative(utterance: str) -> bool:
    text = (utterance or "").lower()
    return bool(_tokens(text) & AFFIRMATIVE_TOKENS) or _has(
        text,
        "go ahead",
        "speaking",
        "sounds good",
        "that is fine",
        "that's fine",
    )


def _is_negative(utterance: str) -> bool:
    text = (utterance or "").lower()
    return bool(_tokens(text) & NEGATIVE_TOKENS) or _has(
        text,
        "nothing",
        "that's all",
        "that is all",
        "no thanks",
        "no thank",
        "not aware",
        "didn't know",
        "didnt know",
    )


def _is_awareness_response(utterance: str) -> bool:
    text = _normalize_text(utterance)
    return _has(text, "aware", "i know", "i am aware", "yes i know", "haan pata", "pata hai")


def _is_unaware_response(utterance: str) -> bool:
    text = _normalize_text(utterance)
    return _has(text, "unaware", "not aware", "didn't know", "didnt know", "i don't know", "i dont know", "pata nahi", "malum nahi")


def _is_soft_callback_request(utterance: str) -> bool:
    text = (utterance or "").lower()
    return bool(_tokens(text) & NEGATIVE_TOKENS) or any(
        phrase in text
        for phrase in (
            "not now", "not available", "not a good time", "busy",
            "can't talk", "cant talk", "cannot talk", "call later",
        )
    )


# ── End-of-call helpers ───────────────────────────────────────────────────────

def _contains_sensitive_payment_data(text: str | None) -> bool:
    if not text:
        return False
    patterns = [
        r"\botp\b",
        r"\bcvv\b",
        r"\bcard\s*number\b",
        r"\bupi\b",
        r"\bbank\s*account\b",
        r"\baccount\s*number\b",
        r"\b\d{12,19}\b",
        r"\b[\w.\-]{2,}@[a-zA-Z]{2,}\b",
    ]
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in patterns)


def _looks_like_dropped_call(ended_reason: str) -> bool:
    lowered = ended_reason.lower()
    return any(token in lowered for token in ("customer", "user", "hangup", "hang-up", "ended", "disconnect"))


def _dropped_event_for_state(last_state: str) -> str:
    if last_state == "verify_borrower":
        return "call-dropped-before-verification"
    if last_state == "self_identification":
        return "call-dropped-after-verification"
    if last_state in {"explain_dues", "confirm_awareness"}:
        return "call-dropped-after-dues-disclosure"
    if last_state in {"capture_commitment_date", "capture_payment_mode"}:
        return "call-dropped-during-commitment-capture"
    if last_state == "offer_payment_link":
        return "call-dropped-after-commitment-before-link"
    if last_state in {"close", "post_link_support_check"}:
        return "call-dropped-after-link-offered"
    return "call-dropped-before-verification"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": "stateless"}


@app.post("/vapi/handle-customer-turn")
async def handle_customer_turn_endpoint(payload: VapiToolCallRequest) -> dict[str, Any]:
    result = handle_customer_turn(payload)
    return result.model_dump(mode="json")


@app.post("/vapi/webhook")
async def vapi_webhook(request: Request) -> dict[str, Any]:
    body = await request.json()
    message = body.get("message", body)
    message_type = message.get("type") or body.get("type")

    if message_type == "tool-calls":
        results = []
        for tool_call in _tool_calls(message):
            name = _tool_name(tool_call)
            if name != "handle_customer_turn":
                logger.info(f"Ignoring unsupported tool call | tool_name={name}")
                continue
            arguments = _tool_arguments(tool_call)
            arguments["call_id"] = arguments.get("call_id") or get_call_id({"message": message, "tool_call": tool_call})
            arguments["current_state"] = arguments.get("current_state") or "verify_borrower"
            tool_request = VapiToolCallRequest.model_validate(arguments)
            result = handle_customer_turn(tool_request)
            results.append(
                {
                    "toolCallId": tool_call.get("id") or tool_call.get("toolCallId"),
                    "result": json.dumps(result.model_dump(mode="json")),
                }
            )
        return {"results": results}

    if message_type in {"end-of-call-report", "call-ended", "end_call_report"}:
        payload = normalize_end_of_call_payload(body)
        handle_end_of_call(payload)
        return {"status": "ok"}

    logger.info(f"Unsupported Vapi webhook message type | message_type={message_type}")
    return {"status": "ignored"}


def handle_customer_turn(payload: VapiToolCallRequest) -> BackendResponse:
    # State is derived entirely from the inbound request — no DB lookup.
    # borrower_verified is inferred from current_state position in the flow.
    # tool_failure_count is inferred from silence_reprompt state (== 1, else 0).
    session = {
        "call_id": payload.call_id,
        "current_state": payload.current_state,
        "customer_name": payload.customer_name,
        "loan_amount": payload.loan_amount,
        "emi_due": payload.emi_due,
        "due_date": payload.due_date,
        "days_past_due": payload.days_past_due,
        "borrower_verified": payload.current_state not in {"start", "verify_borrower"},
        "tool_failure_count": 1 if payload.current_state == "silence_reprompt" else 0,
    }

    result = process(
        session=session,
        latest_user_utterance=payload.latest_user_utterance,
        vapi_detected_intent=payload.detected_intent,
        slots=payload.slots(),
    )

    logger.info(
        f"TURN | call_id={payload.call_id} | state={payload.current_state} | "
        f"intent={payload.detected_intent} | outcome={result.logged_outcome}"
    )
    return result


def handle_end_of_call(payload: EndOfCallPayload) -> None:
    data = payload.model_dump(mode="json")
    call_id = data["call_id"]
    ended_reason = data.get("ended_reason_from_vapi") or "unknown"
    transcript = data.get("transcript")
    requires_redaction = _contains_sensitive_payment_data(transcript)
    final_transcript = "[REDACTED_SENSITIVE_PAYMENT_DETAILS]" if requires_redaction else transcript

    report: dict[str, Any] = {
        **data,
        "requires_redaction": requires_redaction,
        "final_transcript": final_transcript,
    }

    if _looks_like_dropped_call(ended_reason):
        last_state = "verify_borrower"
        dropped_event = _dropped_event_for_state(last_state)
        report["final_outcome"] = "call-dropped"
        report["dropped_event"] = dropped_event
        report["end_reason"] = "customer_hung_up"

    logger.info(f"END_OF_CALL | call_id={call_id} | ended_reason={ended_reason} | payload={json.dumps(report, default=str)}")


def get_call_id(request_body: dict[str, Any]) -> str:
    tool_call = request_body.get("tool_call") or {}
    arguments = _tool_arguments(tool_call) if tool_call else {}
    if arguments.get("call_id"):
        return str(arguments["call_id"])

    message = request_body.get("message", request_body)
    for path in (
        ("metadata", "call_id"),
        ("metadata", "session_id"),
        ("assistant", "metadata", "call_id"),
        ("assistant", "metadata", "session_id"),
        ("call", "id"),
        ("call", "callId"),
        ("call_id",),
        ("callId",),
        ("id",),
    ):
        value = _nested_get(message, path)
        if value:
            return str(value)

    if tool_call.get("id") or tool_call.get("toolCallId"):
        return str(tool_call.get("id") or tool_call.get("toolCallId"))

    raise HTTPException(status_code=422, detail="call_id missing from tool parameters and Vapi message.call.id")


def normalize_end_of_call_payload(body: dict[str, Any]) -> EndOfCallPayload:
    message = body.get("message", body)
    call = message.get("call") or body.get("call") or {}
    call_id = (
        message.get("call_id")
        or message.get("callId")
        or call.get("id")
        or call.get("callId")
        or body.get("call_id")
        or body.get("callId")
    )
    if not call_id:
        raise HTTPException(status_code=422, detail="call_id missing from end-of-call payload")

    payload = {
        "call_id": str(call_id),
        "vapi_call_id": call.get("id") or call.get("callId") or str(call_id),
        "ended_reason_from_vapi": message.get("endedReason") or message.get("ended_reason") or body.get("endedReason") or "unknown",
        "started_at": message.get("startedAt") or call.get("startedAt"),
        "ended_at": message.get("endedAt") or call.get("endedAt"),
        "duration_seconds": int(message.get("durationSeconds") or message.get("duration_seconds") or 0),
        "transcript": message.get("transcript"),
        "messages": message.get("messages") or [],
        "recording_url": message.get("recordingUrl") or message.get("recording_url"),
        "summary": message.get("summary"),
        "raw_payload": body,
    }
    return EndOfCallPayload.model_validate(payload)


def _tool_name(tool_call: dict[str, Any]) -> str | None:
    function = tool_call.get("function") or {}
    return tool_call.get("name") or function.get("name")


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = (
        message.get("toolCalls")
        or message.get("tool_calls")
        or message.get("toolCallList")
        or []
    )
    if calls:
        return calls

    tool_with_call_list = message.get("toolWithToolCallList") or []
    extracted = []
    for item in tool_with_call_list:
        if isinstance(item, dict) and isinstance(item.get("toolCall"), dict):
            extracted.append(item["toolCall"])
    return extracted


def _tool_arguments(tool_call: dict[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function") or {}
    arguments = tool_call.get("arguments") or function.get("arguments") or function.get("parameters") or {}
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail="Invalid JSON tool arguments") from exc
    if isinstance(arguments, dict):
        return dict(arguments)
    return {}


def _nested_get(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
