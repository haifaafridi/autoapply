"""
models.py — Data shapes that flow between modules.

Why Pydantic models?
  Instead of passing plain dicts around (where a typo silently gives you None
  instead of an error), we define the exact shape of data each module expects.
  Pydantic validates incoming data and raises a clear error if something's wrong.

Think of these as contracts:
  - FormField:    what the scanner PRODUCES
  - FieldMapping: what the mapper PRODUCES and the executor CONSUMES
  - The LLM* models: used in Phase 2 for the Claude response
"""

from typing import Optional
from pydantic import BaseModel, Field, computed_field


# ─────────────────────────────────────────────────────────────────────────────
# Scanner output
# ─────────────────────────────────────────────────────────────────────────────

class FormField(BaseModel):
    """
    Represents a single form field found on the page.
    Produced by browser/scanner.py.
    """

    # Position in the scanned list — also used as part of the CSS selector
    idx: int

    # Raw HTML attributes (may be None if the HTML didn't include them)
    element_id: Optional[str] = None
    name: Optional[str] = None

    # What kind of field this is:
    # text, email, tel, url, number, password → fill with text
    # textarea                                → fill with text (multiline)
    # select                                  → pick from dropdown
    # checkbox                                → check or uncheck
    # radio                                   → check one option
    # file                                    → upload a file
    field_type: str

    # The label is what a human reads to know what the field is for.
    # e.g. "First Name", "Upload your resume", "Are you authorized to work?"
    label: Optional[str] = None

    # Placeholder text inside the field (used as fallback if no label exists)
    placeholder: Optional[str] = None

    # Whether the form marks this field as required
    required: bool = False

    # The CSS selector we'll use to find this element later when filling
    selector: str

    # For <select> elements: the list of available options
    options: Optional[list[str]] = None

    @computed_field
    @property
    def display_label(self) -> str:
        """
        The best human-readable name for this field, used in logs.
        Falls back through label → placeholder → name → id → generic.
        """
        return (
            self.label
            or self.placeholder
            or self.name
            or self.element_id
            or f"field_{self.idx}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Mapper output / Executor input
# ─────────────────────────────────────────────────────────────────────────────

class FieldMapping(BaseModel):
    """
    The decision of what to do with a single form field.

    Phase 1: built by the keyword-rule function in main.py
    Phase 2: built by the LLM in agent/mapper.py

    The executor reads this and acts on it — it doesn't care WHERE
    the mapping came from, only what it says.
    """

    # Which element to target (the data-autoapply-idx selector)
    selector: str

    # How to fill it (determines which Playwright method to use)
    field_type: str

    # Label kept here so executor can log human-readable names
    label: Optional[str] = None

    # The value to put in the field.
    # We use str for everything — the executor converts "true"/"false" for checkboxes.
    value: str = ""

    # --- Decision flags ---

    # True = executor should skip this field entirely
    skipped: bool = False

    # Why it was skipped (shown in the run log and summary)
    skip_reason: Optional[str] = None

    # True = this field needs the human to fill it (e.g. essay questions)
    # The executor skips it AND it appears in the "needs your attention" list
    needs_human: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 models (stubs — used when we add the LLM mapper)
# ─────────────────────────────────────────────────────────────────────────────

class LLMFieldInstruction(BaseModel):
    """
    Phase 2: One instruction from the LLM for a single field.
    The LLM returns a JSON array of these.
    """
    selector: str
    value: str
    needs_human: bool = False

    # The LLM explains its reasoning — great for debugging
    reason: Optional[str] = None

    # How confident the LLM is (high / medium / low)
    confidence: str = Field(default="high", pattern="^(high|medium|low)$")


class LLMResponse(BaseModel):
    """
    Phase 2: The complete structured JSON the LLM must return.
    Pydantic validates this before we trust it.
    """
    instructions: list[LLMFieldInstruction]

    # Any overall notes the LLM wants to surface (e.g. "I skipped the essay")
    notes: Optional[str] = None
