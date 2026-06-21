"""
main.py — CLI entry point and Phase 1 hardcoded field mapper.

Run with:
  python main.py --url <job_url> --profile profile.json

How this file is organised:
  1. load_profile()           — reads profile.json from disk
  2. build_hardcoded_mapping()— Phase 1 mapper: keyword rules → FieldMappings
  3. save_run_log()           — writes everything to logs/
  4. print_summary()          — human-readable terminal summary
  5. run()                    — the main orchestration loop
  6. main()                   — argparse CLI, calls run()

In Phase 2, step 2 is replaced by a call to agent/mapper.py.
Everything else stays the same — that's the value of the FieldMapping interface.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

import config
from models import FormField, FieldMapping
from browser.scanner import scan_fields
from browser.executor import execute_mappings, find_and_click_next, detect_submit_button
from agent.mapper import map_fields_with_llm


# ─────────────────────────────────────────────────────────────────────────────
# Profile loader
# ─────────────────────────────────────────────────────────────────────────────

def load_profile(path: str) -> dict:
    """
    Read the user's profile.json from disk.
    Exits with a clear error message if the file is missing or malformed.
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[Error] Profile file not found: {path}")
        print("        Make sure you're running from the autoapply/ directory")
        print("        and that you've copied profile.json with your own data.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"[Error] Your profile.json has a syntax error: {e}")
        print("        Use a JSON validator (e.g. jsonlint.com) to find the problem.")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: Hardcoded keyword-rule mapper
#
# This is INTENTIONALLY simple. It shows the shape of the problem:
# "given a list of fields and a profile, produce a list of FieldMappings."
#
# Phase 2 replaces this function body with:
#   from agent.mapper import map_fields_with_llm
#   return map_fields_with_llm(fields, profile)
# ─────────────────────────────────────────────────────────────────────────────

def build_hardcoded_mapping(fields: list[FormField], profile: dict) -> list[FieldMapping]:
    """
    Map form fields to profile values using keyword matching.

    How it works:
      For each field, we take its label text and name attribute,
      lowercase both, and check if any of our keyword patterns appear.
      The first matching rule wins.

    Limitations (which Phase 2's LLM solves):
      - A label like "Your contact number" won't match "phone"
      - Dropdown values must match exactly
      - Can't handle conditional logic ("if country=US, show state dropdown")
      - Can't fill essay questions sensibly
    """
    # ── Unpack profile for easy access ──────────────────────────────────────
    personal = profile.get("personal", {})
    location = personal.get("location", {})
    education = profile.get("education", {})

    # ── Rules: (list of keywords, value to use) ─────────────────────────────
    #
    # The keywords are checked against the COMBINED string of:
    #   field.label.lower() + " " + field.name.lower()
    #
    # Order matters: put more specific rules before general ones.
    # e.g. "first name" before "name" so "First Name" doesn't get the full name.
    RULES: list[tuple[list[str], str]] = [

        # ── Name ──────────────────────────────────────────────────────────
        (["first name", "firstname", "given name", "first_name"],
         personal.get("first_name", "")),

        (["last name", "lastname", "surname", "family name", "last_name"],
         personal.get("last_name", "")),

        (["full name", "fullname", "your name", "legal name"],
         personal.get("full_name", "")),

        # ── Contact ───────────────────────────────────────────────────────
        (["email", "e-mail", "email address"],
         personal.get("email", "")),

        (["phone", "telephone", "mobile", "cell", "contact number"],
         personal.get("phone", "")),

        # ── Location ──────────────────────────────────────────────────────
        (["city", "town"],
         location.get("city", "")),

        (["state", "province", "region", "county"],
         location.get("state", "")),

        (["country"],
         location.get("country", "")),

        (["zip", "postal", "postcode", "post code"],
         location.get("zip", "")),

        (["address", "street"],
         location.get("city", "")),   # fallback: just city

        # ── Online presence ───────────────────────────────────────────────
        (["linkedin"],
         personal.get("linkedin", "")),

        (["github"],
         personal.get("github", "")),

        (["portfolio", "personal site", "personal url", "website", "blog"],
         personal.get("portfolio", "")),

        # ── Education ─────────────────────────────────────────────────────
        (["university", "school", "college", "institution", "alma mater"],
         education.get("university", "")),

        (["degree", "qualification", "level of education", "highest education"],
         education.get("degree", "")),

        (["major", "field of study", "concentration", "discipline", "program"],
         education.get("major", "")),

        (["graduation", "grad year", "expected graduation", "graduating"],
         education.get("graduation_year", "")),

        (["gpa", "grade point", "academic average"],
         education.get("gpa", "")),

        # ── Resume / CV ───────────────────────────────────────────────────
        (["resume", "cv", "curriculum vitae", "upload your resume", "attach"],
         profile.get("resume_path", "")),

        # ── Work authorization ────────────────────────────────────────────
        (["authorized to work", "work authorization", "work auth",
          "eligible to work", "legal to work", "legally authorized"],
         profile.get("work_authorization", "")),

        (["sponsorship", "visa sponsor", "require sponsor",
          "need sponsorship", "will you require"],
         profile.get("requires_sponsorship", "")),

        # ── Other common fields ───────────────────────────────────────────
        (["pronoun", "pronouns", "gender pronoun"],
         profile.get("pronouns", "")),

        (["hear about", "how did you", "referred by", "source"],
         profile.get("how_did_you_hear", "")),

        (["relocate", "willing to move", "open to relocation"],
         profile.get("willing_to_relocate", "")),
    ]

    # ── EEO / voluntary self-identification fields ─────────────────────────
    #
    # These are protected-category questions: gender, race/ethnicity,
    # veteran status, disability status. In most jurisdictions they're
    # legally required to be voluntary, which means a keyword rule should
    # NEVER guess at them — not even correctly, and especially not by
    # accident through a label/name collision with an unrelated rule
    # (e.g. a "Hispanic/Latino?" field accidentally matching the "city"
    # rule because of how a form groups its fields in the DOM).
    #
    # This check runs BEFORE every other rule below and overrides them
    # unconditionally if it matches. It is a safety boundary, not a
    # keyword-accuracy improvement — that's why it's separate from
    # ALWAYS_HUMAN_LABELS rather than just added to that list.
    #
    # Defined once in config.py and imported here so Phase 1 (this function)
    # and Phase 2 (agent/mapper.py) can never disagree about which fields
    # count as protected-category questions.
    EEO_LABELS = config.EEO_LABELS

    # ── Fields that should ALWAYS be left for the human ───────────────────
    # Even if a rule matches, these field types need human judgment.
    ALWAYS_HUMAN_TYPES = {"textarea"}

    # ── Fields whose labels suggest they need a human ─────────────────────
    ALWAYS_HUMAN_LABELS = [
        "cover letter",
        "why do you want",
        "tell us about yourself",
        "describe",
        "explain",
        "additional information",
        "anything else",
        "salary",
        "compensation",
        "expected salary",
        "desired salary",
        "comments",
        "essay",
    ]

    # ── Match each field ───────────────────────────────────────────────────
    mappings: list[FieldMapping] = []

    for field in fields:
        # Build the text we'll search for keyword matches
        search_text = " ".join([
            (field.label or "").lower(),
            (field.name or "").lower(),
            (field.placeholder or "").lower(),
            (field.element_id or "").lower(),
        ])

        needs_human = False
        skip_reason = None

        # ── Check 1: EEO self-identification fields (runs first, overrides all) ──
        for eeo_keyword in EEO_LABELS:
            if eeo_keyword in search_text:
                needs_human = True
                skip_reason = (
                    f"EEO self-identification field (matched '{eeo_keyword}') "
                    "— always left for you to answer, never auto-filled"
                )
                break

        # ── Check 2: field type always needs a human ──────────────────────
        if not needs_human and field.field_type in ALWAYS_HUMAN_TYPES:
            needs_human = True
            skip_reason = f"field type '{field.field_type}' always needs human input"

        # ── Check 3: label suggests a human is needed ─────────────────────
        if not needs_human:
            for human_keyword in ALWAYS_HUMAN_LABELS:
                if human_keyword in search_text:
                    needs_human = True
                    skip_reason = f"label suggests human input needed (matched '{human_keyword}')"
                    break

        if needs_human:
            mappings.append(FieldMapping(
                selector=field.selector,
                field_type=field.field_type,
                label=field.display_label,
                value="",
                skipped=True,
                skip_reason=skip_reason,
                needs_human=True,
            ))
            continue

        # Try to find a keyword match
        matched_value: str | None = None

        for keywords, value in RULES:
            if any(kw in search_text for kw in keywords):
                matched_value = value
                break

        if matched_value is not None and matched_value != "":
            # Check if the value is itself flagged as needing human input
            if str(matched_value).upper() == "NEEDS_HUMAN":
                mappings.append(FieldMapping(
                    selector=field.selector,
                    field_type=field.field_type,
                    label=field.display_label,
                    value="",
                    skipped=True,
                    skip_reason="profile value marked NEEDS_HUMAN",
                    needs_human=True,
                ))
            else:
                mappings.append(FieldMapping(
                    selector=field.selector,
                    field_type=field.field_type,
                    label=field.display_label,
                    value=str(matched_value),
                ))
        else:
            # No rule matched
            mappings.append(FieldMapping(
                selector=field.selector,
                field_type=field.field_type,
                label=field.display_label,
                value="",
                skipped=True,
                skip_reason="no keyword rule matched — Phase 2 LLM will handle this",
            ))

    return mappings


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def save_run_log(log_data: dict) -> Path:
    """
    Write a complete JSON log of this run to the logs/ folder.

    The log includes:
      - The URL and profile path used
      - Every field found on every page
      - Every mapping decision made
      - The fill results (success/skip/error per field)

    This is your audit trail — you can open it to understand exactly
    what happened and why each field was filled the way it was.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = config.LOGS_DIR / f"run_{timestamp}.json"

    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log_data, f, indent=2, ensure_ascii=False)

    return log_path


# ─────────────────────────────────────────────────────────────────────────────
# Terminal summary
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(all_results: list[dict], all_human_flags: list[dict]) -> None:
    """Print a clear, human-readable summary of the entire run."""

    total_filled = sum(len(r["filled"]) for r in all_results)
    total_skipped = sum(len(r["skipped"]) for r in all_results)
    total_errors = sum(len(r["errors"]) for r in all_results)

    print()
    print("=" * 60)
    print("  AutoApply — Run Complete")
    print("=" * 60)
    print(f"  ✅  Filled:   {total_filled} fields")
    print(f"  ⏭   Skipped:  {total_skipped} fields")
    print(f"  ❌  Errors:   {total_errors} fields")

    if all_human_flags:
        print()
        print("  🙋  Fields needing YOUR input:")
        for item in all_human_flags:
            print(f"      • {item['label']}")
            if item.get("reason"):
                print(f"        ({item['reason']})")

    if total_errors > 0:
        print()
        print("  ⚠️   Check the log file for details on errors.")
        print("       Common causes: field not found, dropdown value mismatch.")

    print("=" * 60)


# ─────────────────────────────────────────────────────────────────────────────
# Main orchestration loop
# ─────────────────────────────────────────────────────────────────────────────

def run(url: str, profile_path: str, auto_submit: bool = False, use_llm: bool = True) -> None:
    """
    The main application flow:

    For each page of the form:
      1. Scan  → find all visible fields
      2. Map   → decide what value goes in each field
      3. Fill  → execute the mappings in the browser

    Then either:
      - Click "Next" and repeat, or
      - Detect the final submit button and pause for review
    """
    profile = load_profile(profile_path)

    print()
    print("=" * 60)
    print("  AutoApply — Phase 1")
    print("=" * 60)
    print(f"  URL:     {url}")
    print(f"  Profile: {profile_path}")
    print(f"  Submit:  {'⚠️  AUTO-SUBMIT ENABLED' if auto_submit else '🔒 Paused (review mode)'}")
    print(f"  Mapper:  {'🧠 LLM (Gemini)' if use_llm else '🔧 Keyword rules (Phase 1)'}")
    print("=" * 60)

    # Accumulate data across all pages for the log and summary
    all_results:      list[dict] = []
    all_human_flags:  list[dict] = []
    page_number: int = 0

    log_data = {
        "phase":       "1 — hardcoded mapper",
        "url":         url,
        "profile":     profile_path,
        "timestamp":   datetime.now().isoformat(),
        "auto_submit": auto_submit,
        "pages":       []
    }

    # ── Open the browser ────────────────────────────────────────────────────
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=config.BROWSER_HEADLESS,
            slow_mo=config.BROWSER_SLOW_MO,
        )
        page = browser.new_page()

        print(f"\n[Browser] Navigating to: {url}")
        page.goto(url, wait_until="networkidle")
        print(f"[Browser] Page loaded: {page.title()}")

        # ── Page loop ───────────────────────────────────────────────────────
        while page_number < config.MAX_PAGES:
            page_number += 1
            print(f"\n{'─' * 60}")
            print(f"  Page {page_number}")
            print(f"{'─' * 60}")

            # ── Step 1: Scan ──────────────────────────────────────────────
            fields = scan_fields(page)

            if not fields:
                print(
                    f"\n[Page {page_number}] No form fields found. Stopping.")
                break

            # ── Step 2: Map ───────────────────────────────────────────────
            #
            # 🔄 PHASE 2 CHANGE POINT
            # Replace the line below with:
            #   from agent.mapper import map_fields_with_llm
            #   mappings = map_fields_with_llm(fields, profile)
            #
            if use_llm:
                print(f"\n[Mapper] Sending {len(fields)} fields to Gemini...")
                mappings = map_fields_with_llm(fields, profile)
            else:
                print(f"\n[Mapper] Building field mappings (Phase 1: keyword rules)...")
                mappings = build_hardcoded_mapping(fields, profile)

            # Collect human-needed flags for the final summary
            for m in mappings:
                if m.needs_human:
                    all_human_flags.append({
                        "label":  m.label,
                        "reason": m.skip_reason,
                    })

            # ── Step 3: Fill ──────────────────────────────────────────────
            results = execute_mappings(page, mappings)
            all_results.append(results)

            # ── Log this page ─────────────────────────────────────────────
            log_data["pages"].append({
                "page_number": page_number,
                "page_title":  page.title(),
                "fields_found": len(fields),
                "fields":      [f.model_dump() for f in fields],
                "mappings":    [m.model_dump() for m in mappings],
                "results":     results,
            })

            # ── Step 4: Check for submit button ───────────────────────────
            submit_label = detect_submit_button(page)
            if submit_label:
                print(
                    f"\n[Gate] 🔒 Detected final submit button: '{submit_label.strip()}'")

                if auto_submit:
                    print("[Gate] ⚠️  --submit flag is set.")
                    print("[Gate]    Auto-submit will be implemented in Phase 3.")
                    print("[Gate]    For now, please click the button manually.")
                else:
                    print(
                        "[Gate] The form is filled. Review it in the browser window.")
                    print("[Gate] Fix anything that needs your attention.")
                    print()
                    print("       Press Enter here when you're done reviewing")
                    print("       (or Ctrl+C to cancel without submitting)")
                    input("       > ")

                break   # Stop the page loop — we've reached the end

            # ── Step 5: Go to next page ───────────────────────────────────
            if find_and_click_next(page):
                # Wait a moment for animations/transitions to complete
                page.wait_for_timeout(1500)
            else:
                print(
                    f"\n[Navigator] No 'Next' button found — this appears to be the last page.")
                print("[Gate] Review the form in the browser window.")
                print("       Press Enter to close, or Ctrl+C to cancel.")
                input("       > ")
                break

        # ── Close browser ────────────────────────────────────────────────
        browser.close()

    # ── Save log ─────────────────────────────────────────────────────────
    log_path = save_run_log(log_data)
    print(f"\n[Log] Run log saved to: {log_path.relative_to(config.BASE_DIR)}")

    # ── Print summary ─────────────────────────────────────────────────────
    print_summary(all_results, all_human_flags)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="autoapply",
        description="AutoApply — Automatically fill job application forms",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage — fills the form, pauses before submit
  python main.py --url https://boards.greenhouse.io/company/jobs/123 --profile profile.json

  # Enable auto-submit (Phase 3 — not yet active)
  python main.py --url https://boards.greenhouse.io/company/jobs/123 --profile profile.json --submit

Tips:
  - Find a Greenhouse job at: boards.greenhouse.io
  - Edit profile.json with your real information before running
  - Check logs/ after the run to see what was filled and why
        """
    )

    parser.add_argument(
        "--url",
        required=True,
        help="Direct URL of the job application form page"
    )
    parser.add_argument(
        "--profile",
        required=True,
        help="Path to your profile JSON file (e.g. profile.json)"
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        default=False,
        help="⚠️  Auto-submit the form (Phase 3 only — currently no-op)"
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        default=False,
        help="Use Phase 1 keyword rules instead of the Gemini LLM mapper"
    )

    args = parser.parse_args()
    run(url=args.url, profile_path=args.profile, auto_submit=args.submit, use_llm=not args.no_llm)


if __name__ == "__main__":
    main()
