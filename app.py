import streamlit as st
import pandas as pd
import json, re, time, subprocess, sys, os
from openai import OpenAI
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

st.set_page_config(page_title="Forms Synthetic Test Bot", page_icon="🧪", layout="wide")
st.title("🧪 Google Forms Synthetic Test Bot")
st.write("Hosted version: generate varied synthetic test records, preview them, and—only when the form visibly labels them as synthetic—submit them to your own test form.")
st.info("This app runs browser automation on the hosting server, not on your Mac. Use only your duplicate/test form. Synthetic submissions must remain clearly labelled and must not be represented as genuine research participants.")

with st.sidebar:
    st.header("AI settings")
    api_key = st.text_input("OpenAI API key", type="password")
    model = st.text_input("API model", value="gpt-4.1-mini")
    count = st.number_input("Synthetic records", min_value=1, max_value=100, value=100)
    st.caption("The key is used locally for this session and is not saved in the project.")

url = st.text_input("Google Forms responder URL", placeholder="https://docs.google.com/forms/d/e/.../viewform")
context = st.text_area("Synthetic respondent context", value="Create varied fictional employee profiles for testing a questionnaire about transformational leadership, employee training, supervisor support, organizational identification, and employee engagement.")
marker_title = st.text_input("Required synthetic-label question", value="Synthetic test response?")
marker_option = st.text_input("Required synthetic-label answer", value="yes")

@st.cache_resource
def ensure_chromium():
    # Streamlit Cloud starts with a fresh container; install the Playwright browser once.
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Playwright could not install Chromium. "
            + (result.stderr[-2500:] or result.stdout[-2500:])
        )

def question_cards(page):
    """Return only top-level Google Forms question cards, in visible DOM order."""
    cards = page.locator('div[role="listitem"]')
    if cards.count() == 0:
        cards = page.locator('div.Qr7Oae')
    return cards


def question_title(block):
    """Read the prompt only; never use the first line of the whole card as a prompt."""
    for selector in ('.M7eMe', '[role="heading"]'):
        loc = block.locator(selector)
        for i in range(loc.count()):
            try:
                value = loc.nth(i).inner_text(timeout=500).strip()
                if value:
                    # Google Forms displays an asterisk for required questions.
                    return re.sub(r'\s*\*\s*$', '', value).strip()
            except Exception:
                pass
    return ""


def page_questions(page, page_index=0):
    """Extract actual question cards only. Do not scan nested data-params/options."""
    out = []
    cards = question_cards(page)
    for i in range(cards.count()):
        block = cards.nth(i)
        qtext = question_title(block)
        if not qtext:
            continue

        radios = block.locator('[role="radio"]')
        checks = block.locator('[role="checkbox"]')
        options = []
        control_type = "text"

        if radios.count():
            control_type = "choice"
            choices = radios
        elif checks.count():
            control_type = "checkbox"
            choices = checks
        else:
            choices = None

        if choices is not None:
            for j in range(choices.count()):
                choice = choices.nth(j)
                label = (
                    choice.get_attribute("aria-label")
                    or choice.get_attribute("data-value")
                    or choice.inner_text(timeout=300)
                    or ""
                ).strip()
                if label and label not in options:
                    options.append(label)
        else:
            fields = block.locator(
                'textarea, input:not([type="hidden"]), [contenteditable="true"]'
            )
            if fields.count() == 0:
                continue

        # Required status: Google Forms normally puts * in the prompt; retain
        # an aria-label fallback for accessibility variants.
        raw_title = ""
        try:
            raw_title = block.locator('.M7eMe').first.inner_text(timeout=300)
        except Exception:
            pass
        required = ("*" in raw_title) or block.locator(
            '[aria-label*="Required"], [aria-label*="required"]'
        ).count() > 0

        out.append({
            "question": qtext,
            "options": options,
            "kind": control_type,
            "required": bool(required),
            "page_index": page_index,
        })
    return out


def page_fingerprint(questions):
    return json.dumps(
        [(q["question"], q["kind"], q["options"]) for q in questions],
        ensure_ascii=False,
        sort_keys=False,
    )


def find_nav_button(page, label):
    """Find a visible Google Forms navigation button by its exact text."""
    loc = page.locator('[role="button"], button')
    for i in range(loc.count()):
        item = loc.nth(i)
        try:
            if item.is_visible() and item.inner_text(timeout=300).strip() == label:
                return item
        except Exception:
            continue
    return None


def complete_required_fields_for_inspection(page, questions, marker_title, marker_option):
    """Temporarily answer required fields so Google Forms permits Next during inspection.

    This runs only in the disposable inspection browser. It never clicks Submit and
    does not change the form or create a response.
    """
    cards = question_cards(page)
    for i, q in enumerate(questions):
        if not q.get("required"):
            continue
        block = cards.nth(i)
        if q["question"].casefold() == marker_title.strip().casefold():
            if q["kind"] != "choice" or marker_option.strip() not in q["options"]:
                raise RuntimeError(
                    "The synthetic-label question must be a required Multiple choice question "
                    f"with the exact option '{marker_option}'."
                )
            click_answer(block, marker_option.strip(), q["options"])
        elif q["kind"] == "choice":
            if not q["options"]:
                raise RuntimeError(f"Required question has no readable options: {q['question']}")
            click_answer(block, q["options"][0], q["options"])
        elif q["kind"] == "text":
            field = block.locator(
                'textarea, input:not([type="hidden"]), [contenteditable="true"]'
            ).first
            field.fill("Synthetic inspection")
        else:
            raise RuntimeError(
                f"Required question type '{q['kind']}' is not supported for safe inspection: {q['question']}"
            )


def inspect_form(form_url, marker_title, marker_option):
    ensure_chromium()
    pages = []
    visited = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(form_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            st.session_state["debug_page_title"] = page.title()

            for page_index in range(20):
                qs = page_questions(page, page_index)
                if not qs:
                    raise RuntimeError(
                        f"No question cards were found on form page {page_index + 1}. "
                        "Check that the responder link opens the full form."
                    )

                fingerprint = page_fingerprint(qs)
                if fingerprint in visited:
                    raise RuntimeError(
                        "The form returned to a page already inspected. "
                        "Navigation was stopped to prevent duplicate questions."
                    )
                visited.add(fingerprint)
                pages.append(qs)

                nxt = find_nav_button(page, "Next")
                if nxt is None:
                    break

                # Google Forms blocks Next when required questions are unanswered.
                # Fill required fields with temporary dummy answers in this inspection
                # session only; never click Submit.
                complete_required_fields_for_inspection(
                    page, qs, marker_title, marker_option
                )
                nxt = find_nav_button(page, "Next")
                if nxt is None:
                    raise RuntimeError("The Next button disappeared after completing required inspection fields.")
                nxt.click()
                page.wait_for_timeout(1400)
                next_qs = page_questions(page, page_index + 1)
                if not next_qs:
                    raise RuntimeError(
                        "Google Forms did not show another question page after Next. "
                        "Check for a sign-in prompt, validation message, or section navigation issue."
                    )
                if page_fingerprint(next_qs) == fingerprint:
                    raise RuntimeError(
                        "The required fields were temporarily completed, but Next still did not change "
                        "the visible questions. The form may have a sign-in restriction or a navigation issue."
                    )
            else:
                raise RuntimeError(
                    "The form has more than 20 pages or navigation did not finish. "
                    "Inspection stopped for safety."
                )
        finally:
            browser.close()
    return pages


def generate_batch(client, questions, n, context, model):
    prompt = f"""
Generate exactly {n} DISTINCT synthetic test records for a questionnaire. They are fictional test data, not real employee feedback.
Context: {context}
Questionnaire in exact display order (each item includes exact title, available choices, and type):
{json.dumps(questions, ensure_ascii=False)}
Rules:
- Return ONLY valid JSON: an array of {n} objects.
- Each object must have one key for every exact question title.
- For choice questions, answer with one exact item from options.
- For 1–5 agreement scales, use the exact available numeric option (usually "1" through "5").
- Make profiles internally coherent but varied; avoid repeating the same response pattern.
- Demographic answers must be selected only from the listed choices.
- For the question titled "{marker_title}", answer exactly "{marker_option}".
- Do not invent names, emails, employers, or other personal identifiers.
"""
    response = client.chat.completions.create(
        model=model,
        messages=[{"role":"user","content":prompt}],
        temperature=0.9
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    data = json.loads(raw)
    if not isinstance(data, list) or len(data) != n:
        raise ValueError("AI did not return the requested number of records.")
    return data

def click_answer(block, answer, options):
    radios = block.locator('[role="radio"]')
    if radios.count() == 0:
        raise RuntimeError("Expected a multiple-choice radio question, but no radio choices were found.")
    answer_s = str(answer).strip()
    for idx, opt in enumerate(options):
        if answer_s.lower() == str(opt).strip().lower():
            radios.nth(idx).click()
            return
    raise RuntimeError(f"Answer '{answer_s}' did not match choices {options}")


def submit_one(form_url, pages, record):
    ensure_chromium()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(form_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(700)
            for page_idx, qs in enumerate(pages):
                blocks = question_cards(page)
                by_title = {}
                for i in range(blocks.count()):
                    block = blocks.nth(i)
                    title = question_title(block)
                    if title:
                        by_title.setdefault(title, block)

                for q in qs:
                    title = q["question"]
                    if title not in record:
                        raise RuntimeError(f"Generated record is missing question: {title}")
                    match = by_title.get(title)
                    if match is None:
                        raise RuntimeError(f"Could not locate question on page {page_idx + 1}: {title}")
                    if q["kind"] == "choice":
                        click_answer(match, record[title], q["options"])
                    elif q["kind"] == "text":
                        field = match.locator("textarea, input:not([type='hidden']), [contenteditable='true']").first
                        field.fill(str(record[title]))
                    else:
                        raise RuntimeError(
                            f"Unsupported question type '{q['kind']}' for '{title}'. "
                            "Checkbox questions are not safely supported yet."
                        )

                page.wait_for_timeout(250)
                if page_idx < len(pages) - 1:
                    nxt = find_nav_button(page, "Next")
                    if nxt is None:
                        raise RuntimeError("Could not find Next button while submitting.")
                    nxt.click()
                    page.wait_for_timeout(700)
                else:
                    submit = find_nav_button(page, "Submit")
                    if submit is None:
                        raise RuntimeError("Could not find Submit button.")
                    submit.click()
                    page.wait_for_timeout(1000)
                    if "Your response has been recorded" not in page.locator("body").inner_text():
                        raise RuntimeError("Google Forms did not show its response-recorded confirmation.")
        finally:
            browser.close()


if st.button("Inspect form questions", type="primary", disabled=not url):
    try:
        with st.spinner("Reading each real question card and checking page navigation…"):
            pages = inspect_form(url, marker_title, marker_option)
        st.session_state["pages"] = pages
        # A new inspection invalidates any old preview, because its schema may differ.
        st.session_state.pop("records", None)
        flat = [q for pg in pages for q in pg]
        st.success(f"Detected {len(pages)} page(s) and {len(flat)} question(s).")
        st.dataframe(
            pd.DataFrame([
                {
                    "Page": q["page_index"] + 1,
                    "Question": q["question"],
                    "Type": q["kind"],
                    "Required": "Yes" if q["required"] else "No",
                    "Choices": " | ".join(q["options"]),
                }
                for q in flat
            ]),
            use_container_width=True,
        )

        title_counts = pd.Series([q["question"] for q in flat]).value_counts()
        duplicates = title_counts[title_counts > 1].index.tolist()
        if duplicates:
            st.error(
                "Repeated question titles were detected. This bot uses question titles as record keys, "
                "so generation/submission is locked until each question has a unique title: "
                + ", ".join(duplicates)
            )

        marker = next((q for q in flat if q["question"].casefold() == marker_title.strip().casefold()), None)
        if marker is None:
            st.warning(
                f"Submission is locked: no question titled '{marker_title}' was found."
            )
        elif marker["kind"] != "choice":
            st.warning("The synthetic label must be a Multiple choice (radio button) question, not Checkboxes.")
        elif not marker["required"]:
            st.warning("The synthetic-label question was found, but it is not detected as required. Mark it Required in Google Forms.")
        elif marker_option.strip() not in marker["options"]:
            st.warning(
                f"The synthetic-label question was found, but its exact option '{marker_option}' "
                f"was not detected. Detected options: {marker['options']}"
            )
        else:
            st.success("Synthetic-data label verified: required multiple-choice question and matching option found.")
    except Exception as e:
        st.session_state.pop("pages", None)
        st.session_state.pop("records", None)
        st.error(f"Inspection failed safely: {e}")

if "pages" in st.session_state:
    pages = st.session_state["pages"]
    flat = [q for pg in pages for q in pg]
    titles = [q["question"] for q in flat]
    title_counts = pd.Series(titles).value_counts() if titles else pd.Series(dtype=int)
    has_duplicate_titles = bool((title_counts > 1).any())
    marker = next((q for q in flat if q["question"].casefold() == marker_title.strip().casefold()), None)
    marker_ok = bool(
        marker
        and marker["kind"] == "choice"
        and marker["required"]
        and marker_option.strip() in marker["options"]
    )
    supported_questions = all(q["kind"] in ("choice", "text") for q in flat)
    form_ready = bool(flat) and marker_ok and not has_duplicate_titles and supported_questions

    st.divider()
    st.subheader("Generate test records")
    if not form_ready:
        st.warning(
            "Generation is locked until inspection confirms a required multiple-choice synthetic label, "
            "unique question titles, and supported question types."
        )
    if st.button(
        f"Generate {int(count)} synthetic records",
        disabled=(not api_key or not form_ready),
    ):
        try:
            client = OpenAI(api_key=api_key)
            rows = []
            progress = st.progress(0)
            for start in range(0, int(count), 10):
                batch_n = min(10, int(count) - start)
                rows.extend(generate_batch(client, flat, batch_n, context, model))
                progress.progress(len(rows) / int(count))
            st.session_state["records"] = rows
            st.success(f"Generated {len(rows)} records.")
        except Exception as e:
            st.error(f"Generation failed: {e}")

if "records" in st.session_state and "pages" in st.session_state:
    df = pd.DataFrame(st.session_state["records"])
    st.subheader("Preview")
    st.dataframe(df, use_container_width=True, height=350)
    st.download_button(
        "Download synthetic records CSV",
        df.to_csv(index=False).encode("utf-8-sig"),
        "synthetic_test_responses.csv",
        "text/csv",
    )
    st.divider()
    st.subheader("Submit to your duplicate test form")
    if not marker_ok:
        st.error(
            f"Submission remains disabled until '{marker_title}' is detected as a required "
            f"multiple-choice question with option '{marker_option}'."
        )
    confirm = st.checkbox(
        "I confirm this is my duplicate/test form, and all submitted records are explicitly labelled synthetic."
    )
    if st.button(
        "Submit synthetic responses to Google Forms",
        type="primary",
        disabled=(not form_ready or not confirm),
    ):
        done, errors = 0, []
        bar = st.progress(0)
        status = st.empty()
        for i, row in enumerate(st.session_state["records"]):
            try:
                submit_one(url, pages, row)
                done += 1
                status.write(f"Submitted {done} of {len(st.session_state['records'])}")
            except Exception as e:
                errors.append({"record": i + 1, "error": str(e)})
                status.write(f"Stopped at record {i + 1}: {e}")
                break
            bar.progress((i + 1) / len(st.session_state["records"]))
            time.sleep(1.5)
        if done:
            st.success(f"Confirmed submissions: {done}")
        if errors:
            st.error(
                "The run stopped after an error. Review the error below before retrying; "
                "do not blindly resubmit."
            )
            st.dataframe(pd.DataFrame(errors), use_container_width=True)


st.divider()
st.markdown("""
### Install and run on Mac
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
streamlit run app.py
```
The app opens in your browser. The actual form-filling browser window opens during submission. Keep the Mac awake and do not close the browser.

**Notes:** This prototype supports standard Google Forms multiple-choice/radio questions and text inputs. It may need adjustment for grids, dropdowns, file uploads, CAPTCHA, or unusual form layouts. It checks for the synthetic-label question before enabling submission.
""")
