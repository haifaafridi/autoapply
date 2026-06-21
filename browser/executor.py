"""
browser/executor.py — Takes a list of FieldMappings and fills the form.

Each field type needs a different Playwright method:
  - Text fields       → locator.fill(value)
  - Dropdowns         → locator.select_option(label=value)
  - Checkboxes/Radio  → locator.check() or locator.uncheck()
  - File inputs       → locator.set_input_files(path)

Why not just set the HTML value attribute directly?
  Modern forms (React, Vue, Angular) track their state in JavaScript, not just
  in the DOM. Playwright's fill() and check() dispatch the right browser events
  (input, change, blur) so the form's JavaScript state stays in sync.
  Direct DOM manipulation would often cause form validation to miss the change.
"""

import time
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

import config
from models import FieldMapping


# ─────────────────────────────────────────────────────────────────────────────
# Fill a single field
# ─────────────────────────────────────────────────────────────────────────────

def fill_field(page: Page, mapping: FieldMapping) -> bool:
    """
    Fill one form field according to its FieldMapping.

    Returns:
        True  — field was successfully filled
        False — field was skipped or an error occurred
    """

    # ── Skip checks ─────────────────────────────────────────────────────────

    if mapping.needs_human:
        print(f"  [🙋] '{mapping.label}' → needs human input, skipping")
        return False

    if mapping.skipped:
        print(f"  [⏭ ] '{mapping.label}' → {mapping.skip_reason or 'skipped'}")
        return False

    if not mapping.value and mapping.value != "false":
        # Empty value AND it's not an intentional "uncheck" → skip
        print(f"  [⏭ ] '{mapping.label}' → no value to fill")
        return False

    # ── Find the element ─────────────────────────────────────────────────────

    try:
        locator = page.locator(mapping.selector)

        # Wait up to DEFAULT_TIMEOUT_MS for the element to become visible.
        # Some fields appear after JavaScript runs (e.g. conditional fields).
        locator.wait_for(
            state='visible',
            timeout=config.DEFAULT_TIMEOUT_MS
        )

    except PlaywrightTimeoutError:
        print(f"  [❌] '{mapping.label}' → element not found or not visible")
        print(f"       selector: {mapping.selector}")
        return False

    # ── Fill based on field type ─────────────────────────────────────────────

    field_type = mapping.field_type
    value = mapping.value

    try:

        if field_type in ('text', 'email', 'tel', 'url', 'number',
                          'search', 'password', 'textarea'):
            # fill() clears existing content then types the new value.
            # It also fires the input/change events that React etc. listen to.
            locator.fill(value)
            print(f"  [✅] '{mapping.label}' ← '{_truncate(value)}'")

        elif field_type == 'select':
            # Dropdowns: try matching by visible text first (what a human sees),
            # then by the underlying value attribute (what the server receives).
            try:
                locator.select_option(label=value)
                print(f"  [✅] '{mapping.label}' ← '{value}' (by label)")
            except Exception:
                try:
                    locator.select_option(value=value)
                    print(f"  [✅] '{mapping.label}' ← '{value}' (by value)")
                except Exception:
                    # Neither worked — log it so the human can fix it
                    print(
                        f"  [⚠️ ] '{mapping.label}' → could not select '{value}'")
                    print(f"       The form may not have this exact option.")
                    return False

        elif field_type == 'checkbox':
            # "true", "yes", "on" → check; anything else → uncheck
            should_check = value.lower() in ('true', 'yes', '1', 'on', 'checked')
            if should_check:
                locator.check()
                print(f"  [✅] '{mapping.label}' ← checked ✓")
            else:
                locator.uncheck()
                print(f"  [✅] '{mapping.label}' ← unchecked")

        elif field_type == 'radio':
            # For radio buttons, we check the specific option the mapping points to.
            # The selector should already target the correct radio input.
            locator.check()
            print(f"  [✅] '{mapping.label}' ← selected (radio)")

        elif field_type == 'file':
            # File uploads: Playwright sets the file directly without opening
            # the OS file picker (which we couldn't control).
            file_path = Path(value)
            if not file_path.exists():
                print(f"  [❌] '{mapping.label}' → file not found: {file_path}")
                print(f"       Update 'resume_path' in your profile.json")
                return False
            locator.set_input_files(str(file_path.resolve()))
            print(f"  [✅] '{mapping.label}' ← '{file_path.name}' (uploaded)")

        else:
            print(
                f"  [⏭ ] '{mapping.label}' → unhandled field type '{field_type}'")
            return False

    except PlaywrightTimeoutError:
        print(f"  [❌] '{mapping.label}' → timed out during fill")
        return False
    except Exception as e:
        print(f"  [❌] '{mapping.label}' → unexpected error: {e}")
        return False

    # Small pause between fields — helps React/Vue state settle and
    # makes the filling look more natural in the browser window
    time.sleep(0.25)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Fill all fields on a page
# ─────────────────────────────────────────────────────────────────────────────

def execute_mappings(page: Page, mappings: list[FieldMapping]) -> dict:
    """
    Fill all mapped fields on the current page.

    Returns a summary dict with three lists:
      filled  — successfully filled fields
      skipped — skipped fields (with reason)
      errors  — fields that failed
    """
    results: dict[str, list] = {
        "filled":  [],
        "skipped": [],
        "errors":  []
    }

    print(f"\n[Executor] Processing {len(mappings)} fields...")
    print("─" * 40)

    for mapping in mappings:
        success = fill_field(page, mapping)

        # Categorise the outcome for the log
        if mapping.needs_human or mapping.skipped:
            results["skipped"].append({
                "label":  mapping.label,
                "reason": mapping.skip_reason or ("needs human" if mapping.needs_human else "skipped"),
                "needs_human": mapping.needs_human
            })
        elif success:
            results["filled"].append({
                "label": mapping.label,
                "value": _truncate(mapping.value)
            })
        else:
            results["errors"].append({
                "label":    mapping.label,
                "selector": mapping.selector
            })

    print("─" * 40)
    print(f"[Executor] ✅ {len(results['filled'])} filled  "
          f"⏭ {len(results['skipped'])} skipped  "
          f"❌ {len(results['errors'])} errors")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Navigation helpers
# ─────────────────────────────────────────────────────────────────────────────

def find_and_click_next(page: Page) -> bool:
    """
    Look for a 'Next' or 'Continue' button and click it.

    We check several text patterns because different ATS platforms
    use different button labels. The order matters — more specific
    patterns first to avoid false positives.

    Returns:
        True  — found and clicked a next button
        False — no next button found (probably the last page)
    """
    NEXT_PATTERNS = [
        "button:has-text('Next Step')",
        "button:has-text('Next Page')",
        "button:has-text('Continue')",
        "button:has-text('Next')",
        "[type='submit']:has-text('Next')",
        "[type='submit']:has-text('Continue')",
        "a:has-text('Next')",
        "a:has-text('Continue')",
    ]

    for pattern in NEXT_PATTERNS:
        try:
            btn = page.locator(pattern).first
            if btn.count() > 0 and btn.is_visible():
                label = btn.text_content() or pattern
                print(f"\n[Navigator] Clicking: '{label.strip()}'")
                btn.click()
                # Wait for the next page's content to load
                page.wait_for_load_state(
                    'networkidle', timeout=config.DEFAULT_TIMEOUT_MS * 2)
                return True
        except Exception:
            continue  # Try the next pattern

    return False


def detect_submit_button(page: Page) -> str | None:
    """
    Check whether the page has what looks like a final submit button.

    Returns:
        The button's text if found, None otherwise.

    We detect the submit button WITHOUT clicking it — that's the whole
    point of the safety gate. We just want to know we've reached the end.
    """
    SUBMIT_PATTERNS = [
        "button:has-text('Submit Application')",
        "button:has-text('Submit My Application')",
        "button:has-text('Apply Now')",
        "button:has-text('Submit')",
        "button:has-text('Apply')",
        "[type='submit']:has-text('Submit')",
        "[type='submit']:has-text('Apply')",
        "input[type='submit']",
    ]

    for pattern in SUBMIT_PATTERNS:
        try:
            btn = page.locator(pattern).first
            if btn.count() > 0 and btn.is_visible():
                return btn.text_content() or "Submit"
        except Exception:
            continue

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _truncate(text: str, max_len: int = 60) -> str:
    """Shorten long strings for display in the terminal."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."
