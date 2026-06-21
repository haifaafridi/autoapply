"""
agent/mapper.py — LLM-powered field mapper (Phase 2).

This replaces the 80-line keyword rules in main.py with a single Claude call.

Why the LLM is better than keyword rules:
  - "Your contact number" → Claude understands this means phone, even without
    the word "phone" in the label. Keywords couldn't catch this.
  - Dropdown fields: Claude sees the available options and picks the exact match.
    Keywords would fill "Yes" into a dropdown whose option is "Yes, I am authorized".
  - EEOC fields: Claude knows "Gender", "Veteran Status" etc. are demographic
    fields and flags them as NEEDS_HUMAN (or picks "Prefer not to self-identify"
    if that option exists).
  - New forms it's never seen: Claude generalises from understanding, not memorised
    patterns.

How it works:
  1. _build_prompt()       Build a prompt with the field list + profile
  2. _call_claude()        Send to claude-sonnet-4-6, get raw text back
  3. _parse_and_validate() Strip markdown fences, JSON.parse, Pydantic validate
  4. _to_field_mappings()  Convert LLMFieldInstruction → FieldMapping

If step 3 fails (malformed JSON), we retry once with the error appended.
If it fails again, we fall back to the Phase 1 keyword rules so the run
doesn't crash completely.
"""

import json
import re

from google import genai
from google.genai import types

import config
from models import (
    FormField,
    FieldMapping,
    LLMFieldInstruction,
    LLMResponse,
)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point — called by main.py
# ─────────────────────────────────────────────────────────────────────────────

def map_fields_with_llm(
    fields: list[FormField],
    profile: dict,
) -> list[FieldMapping]:
    """
    Map form fields to profile values using Claude.

    Args:
        fields:  The list of FormField objects from the scanner.
        profile: The user's profile dict (loaded from profile.json).

    Returns:
        A list of FieldMapping objects — one per field, in the same order.
        The executor consumes these exactly like it consumed Phase 1 mappings.
    """
    if not config.GOOGLE_API_KEY:
        raise ValueError(
            "GOOGLE_API_KEY is not set in your .env file.\n"
            "Get a free key at aistudio.google.com, add it to .env, and restart.\n"
            "Or use --no-llm to fall back to keyword rules (no key needed)."
        )


    prompt = _build_prompt(fields, profile)

    # First attempt
    raw = _call_claude(prompt)
    llm_response, error = _parse_and_validate(raw)

    # One retry if the response was malformed
    if llm_response is None:
        print(f"[Mapper] Response was malformed ({error}). Retrying with error context...")
        retry_prompt = (
            prompt
            + f"\n\nYour previous response caused this parse error: {error}\n"
            + "Please fix the issue and return valid JSON only."
        )
        raw = _call_claude(retry_prompt)
        llm_response, error = _parse_and_validate(raw)

    if llm_response is None:
        # Both attempts failed — fall back to keyword rules rather than crashing
        print(f"[Mapper] ⚠️  LLM failed after retry ({error}). Falling back to keyword rules.")
        from main import build_hardcoded_mapping
        return build_hardcoded_mapping(fields, profile)

    mappings = _to_field_mappings(llm_response, fields)

    # Log how many the LLM decided to fill vs. flag
    filled_count  = sum(1 for m in mappings if not m.skipped and not m.needs_human)
    human_count   = sum(1 for m in mappings if m.needs_human)
    skipped_count = sum(1 for m in mappings if m.skipped and not m.needs_human)
    print(f"[Mapper] Claude decided: {filled_count} fill, {human_count} needs_human, {skipped_count} skip")

    if llm_response.notes:
        print(f"[Mapper] Claude's notes: {llm_response.notes}")

    return mappings


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Build the prompt
# ─────────────────────────────────────────────────────────────────────────────

def _build_prompt(fields: list[FormField], profile: dict) -> str:
    """
    Build the user-turn prompt sent to Claude.

    Design decisions:
    - We include only the field attributes the LLM actually needs
      (not the raw HTML name/id, which would confuse rather than help).
    - We include the full options list for dropdowns — this is what lets
      Claude pick an exact match instead of guessing.
    - We ask for one instruction per field in the same order — this makes
      it easy to correlate responses back to fields even if a selector
      looks confusing.
    - We explicitly list the NEEDS_HUMAN cases so the LLM doesn't try to
      fill demographic or essay fields.
    """

    # Trim the field data to only what's useful for the LLM
    field_summaries = []
    for f in fields:
        summary: dict = {
            "selector":   f.selector,
            "label":      f.display_label,
            "type":       f.field_type,
            "required":   f.required,
        }
        if f.options:
            summary["options"] = f.options   # Critical for dropdowns
        field_summaries.append(summary)

    fields_json  = json.dumps(field_summaries, indent=2)
    profile_json = json.dumps(profile, indent=2)

    return f"""You are filling out a job application form on behalf of a user.

## Form Fields
{fields_json}

## User Profile
{profile_json}

## Your Task
Return a JSON object mapping each field to the value it should receive.
The JSON must follow this exact structure — no other text, no markdown, no explanation:

{{
  "instructions": [
    {{
      "selector": "<copy the selector exactly from the field>",
      "value": "<the value to fill in, or empty string if skipping>",
      "needs_human": false,
      "confidence": "high",
      "reason": "<one sentence: why you chose this value>"
    }}
  ],
  "notes": "<optional: overall observations about this form>"
}}

## Rules

1. Include exactly one instruction per field, in the same order as the fields above.

2. For SELECT / DROPDOWN fields: your value must EXACTLY match one of the strings
   in the field's "options" array. Do not paraphrase or abbreviate.

3. Set needs_human=true (and value="") for ANY of these:
   - Essay or open-ended questions (textarea fields asking for opinions or experience)
   - Cover letters
   - Salary or compensation expectations
   - - Demographic / EEOC fields: gender, race, ethnicity, national origin,
     veteran status, disability status, Hispanic/Latino — IF the user profile
     contains a "demographics" object with a non-empty answer for that exact
     field, use that value directly (it is the user's own self-reported answer,
     never your guess) and set needs_human=false. If the profile has no answer
     for that field, flag needs_human=true. Never invent a demographic answer
     yourself under any circumstance.
   - Anything where guessing would be harmful or inappropriate

4. For FILE fields (resume, CV uploads): use the value from profile.resume_path.

5. For CHECKBOX fields: value="true" to check it, value="false" to leave it unchecked.

6. Never invent information not present in the profile.
   If the profile doesn't have a value for a field, set skipped=true and needs_human=false
   unless it's an essay/demographic field (then needs_human=true).

7. confidence values: "high" = clearly matched, "medium" = reasonable inference,
   "low" = best guess from limited info.

Return only the JSON object. Nothing before or after it."""


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Call the Claude API
# ─────────────────────────────────────────────────────────────────────────────

def _call_claude(prompt: str) -> str:
    """
    Send the prompt to Google Gemini and return the raw text response.

    Why Gemini?
      - Completely free tier at aistudio.google.com (no credit card)
      - 1,500 free requests/day — more than enough for job applications
      - Gemini 2.5 Flash is fast and follows JSON instructions reliably

    The function is still named _call_claude internally so the rest of the
    code doesn't need to change. If you get an Anthropic key later, you
    only need to swap this one function back.
    """
    client = genai.Client(api_key=config.GOOGLE_API_KEY)

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=(
                "You are a precise job application form-filling assistant. "
                "You receive form field metadata and a user profile, and return "
                "a strict JSON mapping. You never fabricate information. "
                "You always return valid JSON and nothing else."
            ),
            temperature=0,
            response_mime_type="application/json",
        ),
    )

    return response.text
# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Parse and validate the response
# ─────────────────────────────────────────────────────────────────────────────

def _parse_and_validate(raw_text: str) -> tuple[LLMResponse | None, str | None]:
    """
    Parse Claude's raw text response into a validated LLMResponse.

    Returns:
        (LLMResponse, None)   — success
        (None, error_message) — failure (caller decides whether to retry)

    We strip markdown code fences because even with explicit instructions,
    LLMs occasionally wrap JSON in ```json ... ```. Rather than let that
    crash everything, we clean it first.
    """
    try:
        # Strip markdown code fences if present
        # Pattern: optional ```json or ``` at start, optional ``` at end
        clean = raw_text.strip()
        clean = re.sub(r'^```(?:json)?\s*', '', clean)
        clean = re.sub(r'\s*```$', '', clean)
        clean = clean.strip()

        # Parse JSON
        data = json.loads(clean)

        # Validate against our Pydantic model
        # If the JSON is valid but missing required fields, this raises
        response = LLMResponse.model_validate(data)
        return response, None

    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"
    except Exception as e:
        return None, f"Validation error: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Convert LLMResponse → list[FieldMapping]
# ─────────────────────────────────────────────────────────────────────────────

def _to_field_mappings(
    llm_response: LLMResponse,
    fields: list[FormField],
) -> list[FieldMapping]:
    """
    Convert the validated LLM instructions into FieldMapping objects.

    We build a lookup from selector → FormField so we can restore the
    field_type (which the executor needs but the LLM doesn't return).

    We also handle the case where the LLM returned fewer instructions than
    fields — any unmatched fields are marked as skipped.
    """
    # Build selector → FormField lookup for easy access
    field_by_selector = {f.selector: f for f in fields}

    mappings: list[FieldMapping] = []

    # Track which selectors the LLM covered
    covered_selectors: set[str] = set()

    for instruction in llm_response.instructions:
        covered_selectors.add(instruction.selector)

        # Look up the original field to get field_type and label
        original_field = field_by_selector.get(instruction.selector)
        field_type = original_field.field_type if original_field else "text"
        label      = original_field.display_label if original_field else instruction.selector

        if instruction.needs_human:
            mappings.append(FieldMapping(
                selector=instruction.selector,
                field_type=field_type,
                label=label,
                value="",
                skipped=True,
                skip_reason=instruction.reason or "LLM flagged as needs human",
                needs_human=True,
            ))
        elif not instruction.value:
            mappings.append(FieldMapping(
                selector=instruction.selector,
                field_type=field_type,
                label=label,
                value="",
                skipped=True,
                skip_reason=instruction.reason or "LLM returned no value",
            ))
        else:
            mappings.append(FieldMapping(
                selector=instruction.selector,
                field_type=field_type,
                label=label,
                value=instruction.value,
            ))

        # Log Claude's reasoning at a verbose level
        confidence_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(
            instruction.confidence, "⚪"
        )
        if instruction.needs_human:
            print(f"  [🙋] {label!r} → NEEDS_HUMAN ({instruction.reason})")
        elif instruction.value:
            print(f"  [{confidence_icon}] {label!r} → '{_truncate(instruction.value)}'  ({instruction.reason})")
        else:
            print(f"  [⏭ ] {label!r} → skipped ({instruction.reason})")

    # Handle any fields the LLM didn't mention (shouldn't happen, but defensive)
    for field in fields:
        if field.selector not in covered_selectors:
            print(f"  [⚠️ ] {field.display_label!r} → not in LLM response, marking as skipped")
            mappings.append(FieldMapping(
                selector=field.selector,
                field_type=field.field_type,
                label=field.display_label,
                value="",
                skipped=True,
                skip_reason="not included in LLM response",
            ))

    return mappings


def _truncate(text: str, max_len: int = 55) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."