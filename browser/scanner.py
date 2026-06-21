"""
browser/scanner.py — Reads a form page and returns structured field data.

The key insight: we can't scan forms reliably from Python alone because
the DOM (the HTML tree) is a live object that JavaScript manipulates.
Playwright lets us run JavaScript INSIDE the browser, giving us direct
access to the real DOM state, including dynamically rendered fields.

How it works:
  1. We write a JavaScript function (SCANNER_JS below) that walks the DOM
     and collects metadata about every visible form field.
  2. We call page.evaluate(SCANNER_JS) — Playwright runs it in the browser
     and returns the result as a Python object.
  3. We validate each result with the FormField Pydantic model.
"""

from playwright.sync_api import Page
from models import FormField
import config


# ─────────────────────────────────────────────────────────────────────────────
# JavaScript that runs inside the browser page
# ─────────────────────────────────────────────────────────────────────────────

SCANNER_JS = """
() => {
    // ── Helper: find the best human-readable label for a field ──────────────
    //
    // HTML forms label their fields in several different ways.
    // We check them in order from most reliable to least.
    function getLabel(el) {

        // 1. aria-label: explicitly set for accessibility
        //    e.g. <input aria-label="First Name">
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();

        // 2. aria-labelledby: points to the ID(s) of other elements
        //    e.g. <input aria-labelledby="label1 hint1">
        const labelledById = el.getAttribute('aria-labelledby');
        if (labelledById) {
            const text = labelledById.split(' ')
                .map(id => {
                    const el = document.getElementById(id);
                    return el ? el.textContent.trim() : '';
                })
                .filter(Boolean)
                .join(' ');
            if (text) return text;
        }

        // 3. <label for="field-id">: the classic HTML way
        //    e.g. <label for="email">Email Address</label> <input id="email">
        if (el.id) {
            const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
            if (label) return label.textContent.trim();
        }

        // 4. The field is wrapped inside a <label>
        //    e.g. <label>First Name <input type="text"></label>
        const parentLabel = el.closest('label');
        if (parentLabel) {
            // Clone it and remove the input itself so we only get the text
            const clone = parentLabel.cloneNode(true);
            clone.querySelectorAll('input, select, textarea').forEach(e => e.remove());
            const text = clone.textContent.trim();
            if (text) return text;
        }

        // 5. A sibling or nearby <label> in the same container
        //    Some form builders put the label and input as siblings in a <div>
        const parent = el.parentElement;
        if (parent) {
            // Walk up one more level if needed
            for (const container of [parent, parent.parentElement]) {
                if (!container) continue;
                const label = container.querySelector('label');
                // Only use it if it doesn't have a `for` pointing elsewhere
                if (label && (!label.htmlFor || label.htmlFor === el.id)) {
                    const text = label.textContent.trim();
                    if (text) return text;
                }
            }
        }

        // 6. Placeholder text as a fallback (not ideal, but better than nothing)
        if (el.placeholder && el.placeholder.trim()) return el.placeholder.trim();

        // 7. Last resort: use the name or id attribute
        return el.name || el.id || '';
    }

    // ── Helper: collect dropdown options ────────────────────────────────────
    function getOptions(el) {
        if (el.tagName.toLowerCase() !== 'select') return null;
        return Array.from(el.options)
            .map(opt => opt.text.trim())
            .filter(text => text && text !== '-- Select --' && text !== 'Select...');
    }

    // ── Helper: determine field type ────────────────────────────────────────
    function getFieldType(el) {
        const tag = el.tagName.toLowerCase();
        if (tag === 'select') return 'select';
        if (tag === 'textarea') return 'textarea';
        // For <input>, use the type attribute (defaults to 'text')
        return (el.type || 'text').toLowerCase();
    }

    // ── Main scan loop ───────────────────────────────────────────────────────
    const fields = [];
    let idx = 0;

    // These are the element types we care about.
    // We explicitly exclude:
    //   [type=hidden]  — not visible to the user
    //   [type=submit]  — buttons, not data fields
    //   [type=button]  — same
    //   [type=reset]   — same
    //   [type=image]   — image submit buttons
    const SELECTOR = [
        'input:not([type="hidden"])',
        'input:not([type="submit"])',
        'input:not([type="button"])',
        'input:not([type="reset"])',
        'input:not([type="image"])',
        'select',
        'textarea'
    ].join(', ');

    // Use a Set to avoid duplicates (the multi-selector above can match once per rule)
    const seen = new Set();

    document.querySelectorAll(
        'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]):not([type="image"]), select, textarea'
    ).forEach(el => {
        // Skip if we've already processed this element
        if (seen.has(el)) return;
        seen.add(el);

        // Skip invisible elements — they're usually for internal form state
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        if (
            style.display === 'none' ||
            style.visibility === 'hidden' ||
            style.opacity === '0' ||
            (rect.width === 0 && rect.height === 0)
        ) {
            return;
        }

        // ✨ THE KEY TRICK: tag each element with a unique index.
        // Later, the executor finds it with [data-autoapply-idx="N"].
        // This survives dynamic re-renders as long as the element stays in the DOM.
        el.setAttribute('data-autoapply-idx', String(idx));

        fields.push({
            idx:        idx,
            element_id: el.id || null,
            name:       el.name || null,
            field_type: getFieldType(el),
            label:      getLabel(el),
            placeholder: el.placeholder || null,
            required:   el.required || el.getAttribute('aria-required') === 'true',
            options:    getOptions(el),
            selector:   `[data-autoapply-idx="${idx}"]`
        });

        idx++;
    });

    return fields;
}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Python function called by main.py
# ─────────────────────────────────────────────────────────────────────────────

def scan_fields(page: Page) -> list[FormField]:
    """
    Scan all visible form fields on the current page.

    Args:
        page: A Playwright Page object (browser tab already navigated to the URL)

    Returns:
        A list of FormField objects, one per visible form field.
        The list is in DOM order (top to bottom, left to right).
    """
    # Wait for the network to go quiet — this ensures dynamic fields have loaded.
    # 'networkidle' means "no more than 0 network requests for 500ms".
    # For very slow pages, you might increase the timeout in config.py.
    page.wait_for_load_state(
        'networkidle', timeout=config.DEFAULT_TIMEOUT_MS * 2)

    # Run our JavaScript scanner in the browser and get the result back as Python
    raw_fields: list[dict] = page.evaluate(SCANNER_JS)

    print(f"\n[Scanner] Found {len(raw_fields)} visible form fields")
    print("─" * 40)

    fields: list[FormField] = []

    for raw in raw_fields:
        try:
            # Pydantic validates the dict — if JS returned something unexpected,
            # this raises a ValidationError with a clear message
            field = FormField(**raw)
            fields.append(field)

            # Print a summary line for each field so you can see what was found
            req_marker = "* " if field.required else "  "
            opts_hint = f" [{len(field.options)} options]" if field.options else ""
            print(
                f"  {req_marker}[{field.field_type:8}] {field.display_label!r}{opts_hint}")

        except Exception as e:
            print(f"  [Scanner] Warning: Skipping malformed field data: {e}")

    print("─" * 40)
    return fields
