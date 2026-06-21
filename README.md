# AutoApply

An AI agent that reads job application forms on real ATS platforms (Greenhouse, etc.), figures out which field is which, and fills them in from your profile — then pauses before the final submit so you always review before anything goes anywhere.

## Why this exists

Retyping the same information into Greenhouse, Lever, and other ATS forms for every job application gets old fast. AutoApply automates the repetitive part while keeping a human in the loop for anything that actually matters.

## How it works

1. **Scan** — Playwright opens the job posting and detects every visible form field
2. **Map** — The field list and your profile are sent to Gemini, which decides what value goes where — including labels that don't exactly match keywords, and exact dropdown option matching
3. **Fill** — Each field is filled in a real, visible browser window
4. **Review** — The agent pauses at the final submit button. Nothing is ever submitted automatically.

## Features

- LLM-powered field mapping (Google Gemini) with a keyword-rule fallback (`--no-llm`, no API key required)
- Never auto-submits — every run pauses for manual review before the final step
- Refuses to guess on sensitive fields: essay questions, cover letters, and salary expectations are always left for you. Demographic/EEOC fields are only filled from your own explicit self-reported answers — never inferred or guessed
- Full audit trail — every run logs each field decision and the reasoning behind it to `logs/`
- Generalizes across different form layouts rather than relying on hardcoded selectors per company

## Tech stack

- Python
- Playwright (browser automation)
- Google Gemini API (`google-genai` SDK)
- Pydantic (data validation)

## Setup

```bash
git clone https://github.com/yourusername/autoapply.git
cd autoapply
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
playwright install chromium

cp .env.example .env           # add your GOOGLE_API_KEY (free at aistudio.google.com)
cp profile.example.json profile.json   # fill in your real info
```

## Usage

```bash
python main.py --url  --profile profile.json
```

Add `--no-llm` to use simple keyword matching instead of Gemini (no API key needed):

```bash
python main.py --url  --profile profile.json --no-llm
```

## Safety design

This tool is built around one rule: **it never decides anything sensitive on your behalf.**

- Essay questions, cover letters, and salary fields are always left blank for you
- Demographic/EEOC fields are filled only if your own profile already contains an explicit answer — the AI never invents one
- The form is never submitted automatically — every run ends with a manual review step

## Project structure
autoapply/

├── main.py                # CLI entry point + keyword-rule fallback mapper

├── agent/

│   └── mapper.py          # LLM-powered field mapper (Gemini)

├── browser/

│   ├── scanner.py         # Finds and reads form fields

│   └── executor.py        # Fills the form in the browser

├── models.py               # Pydantic data models

├── config.py               # Environment/config loading

└── profile.example.json    # Template for your own profile.json

## Roadmap

- [ ] Lever ATS support
- [ ] Auto-submit with an explicit confirmation step
- [ ] Structured run reports

## License

MIT