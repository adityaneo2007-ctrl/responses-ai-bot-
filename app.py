import streamlit as st
import pandas as pd
import json, re, time
from openai import OpenAI
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

st.set_page_config(page_title="Forms Synthetic Test Bot", page_icon="🧪", layout="wide")
st.title("🧪 Google Forms Synthetic Test Bot")
st.write("Generate varied synthetic test records, preview them, and—only when the form visibly labels them as synthetic—submit them to your own test form.")

with st.sidebar:
    st.header("AI settings")
    api_key = st.text_input("OpenAI API key", type="password")
    model = st.text_input("API model", value="gpt-4.1-mini")
    count = st.number_input("Synthetic records", min_value=1, max_value=100, value=100)
    st.caption("The key is used locally for this session and is not saved in the project.")

url = st.text_input("Google Forms responder URL", placeholder="https://docs.google.com/forms/d/e/.../viewform")
context = st.text_area("Synthetic respondent context", value="Create varied fictional employee profiles for testing a questionnaire about transformational leadership, employee training, supervisor support, organizational identification, and employee engagement.")
marker_title = st.text_input("Required synthetic-label question", value="Synthetic test response?")
marker_option = st.text_input("Required synthetic-label answer", value="Yes – AI-generated test data")

def page_questions(page):
    out = []
    blocks = page.locator('div[role="listitem"]').all()
    for i, block in enumerate(blocks):
        try:
            heading = block.locator('[role="heading"]').first
            qtext = heading.inner_text(timeout=1000).strip()
        except Exception:
            qtext = ""
        if not qtext:
            try:
                lines = [x.strip() for x in block.inner_text(timeout=1000).splitlines() if x.strip()]
                qtext = lines[0] if lines else ""
            except Exception:
                qtext = ""
        if not qtext:
            continue
        radios = block.locator('[role="radio"]').all()
        options = []
        for r in radios:
            try:
                label = (r.get_attribute("aria-label") or r.inner_text(timeout=500) or "").strip()
                if not label:
                    label = r.get_attribute("data-value") or ""
                options.append(label)
            except Exception:
                options.append("")
        if options:
            kind = "choice"
        elif block.locator("textarea, input").count():
            kind = "text"
        else:
            continue
        out.append({"question": qtext, "options": options, "kind": kind, "page_index": i})
    return out

def inspect_form(form_url):
    pages = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(form_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1800)
        for _ in range(20):
            qs = page_questions(page)
            pages.append(qs)
            nxt = page.locator('[role="button"]').filter(has_text=re.compile(r"^Next$"))
            if nxt.count() == 0:
                break
            nxt.first.click()
            page.wait_for_timeout(900)
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
        raise RuntimeError("No radio choices found for a question.")
    answer_s = str(answer).strip()
    # Match accessible labels first, then displayed option text, then numeric index.
    for idx, opt in enumerate(options):
        if answer_s.lower() == str(opt).strip().lower():
            radios.nth(idx).click()
            return
    if answer_s.isdigit() and 1 <= int(answer_s) <= radios.count():
        radios.nth(int(answer_s)-1).click()
        return
    raise RuntimeError(f"Answer '{answer_s}' did not match choices {options}")

def submit_one(form_url, pages, record):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(form_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(700)
        for page_idx, qs in enumerate(pages):
            blocks = page.locator('div[role="listitem"]').all()
            qblocks = []
            for b in blocks:
                try:
                    h = b.locator('[role="heading"]').first.inner_text(timeout=500).strip()
                except Exception:
                    try: h = [x.strip() for x in b.inner_text(timeout=500).splitlines() if x.strip()][0]
                    except Exception: h = ""
                if h: qblocks.append((h,b))
            for q in qs:
                if q["question"] not in record:
                    continue
                match = next((b for h,b in qblocks if h == q["question"]), None)
                if match is None:
                    # Fall back to same order among question blocks on this page.
                    ix = qs.index(q)
                    if ix < len(qblocks): match = qblocks[ix][1]
                if match is None:
                    raise RuntimeError(f"Could not locate question: {q['question']}")
                if q["kind"] == "choice":
                    click_answer(match, record[q["question"]], q["options"])
                else:
                    field = match.locator("textarea, input").first
                    field.fill(str(record[q["question"]]))
            page.wait_for_timeout(250)
            if page_idx < len(pages)-1:
                nxt = page.locator('[role="button"]').filter(has_text=re.compile(r"^Next$"))
                if nxt.count() == 0:
                    raise RuntimeError("Could not find Next button.")
                nxt.first.click()
                page.wait_for_timeout(700)
            else:
                submit = page.locator('[role="button"]').filter(has_text=re.compile(r"^Submit$"))
                if submit.count() == 0:
                    raise RuntimeError("Could not find Submit button.")
                submit.first.click()
                page.wait_for_timeout(1000)
                if "Your response has been recorded" not in page.locator("body").inner_text():
                    raise RuntimeError("Google Forms did not show its response-recorded confirmation.")
        browser.close()

if st.button("Inspect form questions", type="primary", disabled=not url):
    try:
        with st.spinner("Reading all form pages and preserving question order…"):
            pages = inspect_form(url)
        st.session_state["pages"] = pages
        flat = [q for pg in pages for q in pg]
        st.success(f"Detected {len(pages)} page(s) and {len(flat)} question(s).")
        st.dataframe(pd.DataFrame([{"Page": i+1, "Question": q["question"], "Type": q["kind"], "Choices": " | ".join(q["options"])} for i,pg in enumerate(pages) for q in pg]), use_container_width=True)
        all_titles = [q["question"] for q in flat]
        if marker_title not in all_titles:
            st.warning(f"Submission is locked: add a required multiple-choice question titled '{marker_title}' with the option '{marker_option}'. This keeps every synthetic submission identifiable in the response data.")
        else:
            st.success("Synthetic-data label question found.")
    except Exception as e:
        st.error(f"Inspection failed: {e}")

if "pages" in st.session_state:
    pages = st.session_state["pages"]
    flat = [q for pg in pages for q in pg]
    titles = [q["question"] for q in flat]
    marker_ok = marker_title in titles and marker_option in next((q["options"] for q in flat if q["question"] == marker_title), [])
    st.divider()
    st.subheader("Generate test records")
    if st.button(f"Generate {int(count)} synthetic records", disabled=not api_key):
        try:
            client = OpenAI(api_key=api_key)
            rows = []
            progress = st.progress(0)
            for start in range(0, int(count), 10):
                batch_n = min(10, int(count)-start)
                rows.extend(generate_batch(client, flat, batch_n, context, model))
                progress.progress(len(rows)/int(count))
            st.session_state["records"] = rows
            st.success(f"Generated {len(rows)} records.")
        except Exception as e:
            st.error(f"Generation failed: {e}")

if "records" in st.session_state:
    df = pd.DataFrame(st.session_state["records"])
    st.subheader("Preview")
    st.dataframe(df, use_container_width=True, height=350)
    st.download_button("Download synthetic records CSV", df.to_csv(index=False).encode("utf-8-sig"), "synthetic_test_responses.csv", "text/csv")
    st.divider()
    st.subheader("Submit to your duplicate test form")
    if not marker_ok:
        st.error(f"Submission remains disabled until the form contains the required synthetic label question: '{marker_title}' with option '{marker_option}'.")
    confirm = st.checkbox("I confirm this is my duplicate/test form, and all submitted records are explicitly labelled synthetic.")
    if st.button("Submit synthetic responses to Google Forms", type="primary", disabled=(not marker_ok or not confirm)):
        done, errors = 0, []
        bar = st.progress(0)
        status = st.empty()
        for i, row in enumerate(st.session_state["records"]):
            try:
                submit_one(url, pages, row)
                done += 1
                status.write(f"Submitted {done} of {len(st.session_state['records'])}")
            except Exception as e:
                errors.append({"record": i+1, "error": str(e)})
                status.write(f"Stopped at record {i+1}: {e}")
                break
            bar.progress((i+1)/len(st.session_state["records"]))
            time.sleep(1.5)
        st.success(f"Confirmed submissions: {done}")
        if errors:
            st.error("The run stopped after an error. Review the error below before retrying; do not blindly resubmit.")
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
