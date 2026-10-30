# Google Forms Synthetic Test Bot (Mac)

This local browser app:
- Inspects a multi-page Google Form and keeps question order.
- Generates up to 100 varied fictional test records using an OpenAI API key.
- Previews and exports records as CSV.
- Can submit records through a visible Chromium browser to a duplicate/test form only after it detects a required synthetic-label question.

## Required form label
Before enabling submissions, add a required multiple-choice question:
- Question: `Synthetic test response?`
- Option: `Yes – AI-generated test data`

This is a safety/quality control so synthetic records are identifiable in the Responses tab. Do not use synthetic records as genuine employee research data.

## Install
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
streamlit run app.py
```

Use a Google Forms responder URL. Enter your own API key in the local app. Do not share it.
