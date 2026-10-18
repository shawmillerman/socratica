import os
import re
import sys
from datetime import datetime, date, timedelta
import hashlib
import shutil
import streamlit as st
import pptx
import pandas as pd
from dotenv import load_dotenv
from langchain_community.document_loaders import DirectoryLoader, PyPDFLoader, TextLoader, Docx2txtLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain.chains import create_history_aware_retriever, create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.documents import Document
from utils.study_package import generate_study_package, format_transcript
from utils.pdf_generator import create_study_worksheet
from utils.ppt_generator import create_practice_deck
from utils.writing_tutor import DraftError, SUPPORTED_TYPES, clean_draft_text, extract_draft_text, load_style_guide, review_draft

load_dotenv()


def _get_api_key():
    """Streamlit Cloud secret first, then the local .env / environment variable."""
    try:
        if "OPENAI_API_KEY" in st.secrets:
            return st.secrets["OPENAI_API_KEY"]
    except Exception:  # no secrets.toml when running locally
        pass
    return os.getenv("OPENAI_API_KEY")


api_key = _get_api_key()
CHROMA_DIR = "./chroma"
HIDDEN_COURSES = {"ba_101"}  # parked course folders: kept in ./data but not shown in the UI
RETRIEVER_K = 10  # chunks per question; high enough to cover most of a textbook chapter for overviews
INDEX_VERSION = "3"  # bump whenever ingestion/chunking changes so cached indexes rebuild
SHEET_SAMPLE_ROWS = 5
OFFER_TOKEN = "[[OFFER_STUDY_PACKAGE]]"
APP_TIMEZONE = "America/New_York"
TERM_START_FILE = "term_start.txt"  # one line per course folder: the first day of Week 1, as YYYY-MM-DD
TERM_WEEKS = 11
EXPORT_FORMATS = {
    "PDF": ("pdf", "application/pdf"),
    "PPTX": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
}


def _pretty(name):
    """Display form of a snake_case folder or file name: 'ba_336' -> 'BA 336', 'excel_textbook' -> 'Excel Textbook'."""
    if re.fullmatch(r"[a-z]{2,4}_\d+", name):
        return name.replace("_", " ").upper()
    return name.replace("_", " ").title() if name == name.lower() else name


def _read_term_start(course_path):
    """First YYYY-MM-DD date in the course's term_start.txt, or None if the file is missing or unreadable."""
    try:
        with open(os.path.join(course_path, TERM_START_FILE), encoding="utf-8") as f:
            match = re.search(r"\d{4}-\d{2}-\d{2}", f.read())
        return date.fromisoformat(match.group(0)) if match else None
    except (OSError, ValueError):
        return None


@st.cache_data(show_spinner=False)
def _schedule_weeks(course_path, signature):
    """Split the course's schedule file(s) into {week_number: section_text} by their 'Week N' headings."""
    weeks = {}
    for root, _dirs, files in os.walk(course_path):
        for name in files:
            lower = name.lower()
            if "schedule" not in lower or not lower.endswith((".docx", ".txt")) or _is_answer_key(os.path.join(root, name)):
                continue
            path = os.path.join(root, name)
            try:
                if lower.endswith(".docx"):
                    text = Docx2txtLoader(path).load()[0].page_content
                else:
                    with open(path, encoding="utf-8") as f:
                        text = f.read()
            except Exception:
                continue
            parts = re.split(r"(?im)^[ \t]*week[ \t]+(\d+)[ \t]*$", text)
            for number, body in zip(parts[1::2], parts[2::2]):
                weeks.setdefault(int(number), re.sub(r"\n\s*\n+", "\n", body).strip())
    return weeks


def _schedule_weeks_for(course_path):
    signature = tuple(
        (n, os.path.getmtime(os.path.join(r, n)))
        for r, _d, fs in os.walk(course_path) for n in fs if "schedule" in n.lower()
    )
    return _schedule_weeks(course_path, signature)


def _week_schedule(course_path, week):
    return _schedule_weeks_for(course_path).get(week, "")


def _calendar_context(course_path):
    """Today's date plus, when the course has a term_start.txt, the current week number."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(APP_TIMEZONE))
    except Exception:
        now = datetime.now()
    today = now.date()
    start = _read_term_start(course_path)
    if start is None:
        term_info = (
            "The course schedule lists topics by week number only, with no calendar dates, so you do NOT know which "
            "week of the term it is right now. Never state or guess the current week. If asked, say so, then either "
            "list what the schedule says for the weeks they name or ask which week they are in."
        )
    elif today < start:
        term_info = f"The term has not started yet; Week 1 begins {start:%A, %B %d, %Y}."
    else:
        week = (today - start).days // 7 + 1
        def _week_line(n):
            first, last = start + timedelta(days=7 * (n - 1)), start + timedelta(days=7 * n - 1)
            gap = (today - last).days
            if gap > 0:
                note = f"Sunday {last:%b %d} was {gap} day{'s' if gap != 1 else ''} ago"
            elif gap == 0:
                note = "Sunday is today"
            else:
                note = f"Sunday {last:%b %d} is in {-gap} day{'s' if gap != -1 else ''}"
            return (
                f"Week {n}: {first:%a %b %d} to {last:%a %b %d, %Y} "
                f"(Wednesday {first + timedelta(days=2):%b %d}, Friday {first + timedelta(days=4):%b %d}; {note})"
            )

        weeks = "\n".join(_week_line(n) for n in range(1, TERM_WEEKS + 1))
        sunday = start + timedelta(days=7 * week - 1)
        section = _week_schedule(course_path, week) if week <= TERM_WEEKS else ""
        def _items(n, text):
            """(name, due_week) for each Apply item in a week; unlabeled sub-items inherit the week's Sunday due date."""
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            apply_at = next((i for i, l in enumerate(lines) if l.lower().startswith("apply")), None)
            explicit = [l for l in lines if re.search(r"\bdue\b", l, re.I) and not re.match(r"(?i)final deadline", l)]
            out = []
            for l in explicit:
                m = re.search(r"due\s+week\s+(\d+)", l, re.I)
                out.append((re.sub(r"\s*\((?:due)[^)]*\)?", "", l, flags=re.I).strip(" :;"), int(m.group(1)) if m else n))
            if apply_at is not None and any(re.search(r"due\s+sunday", l, re.I) for l in explicit):
                named = {name for name, _ in out}
                for l in lines[apply_at + 1:]:
                    if not re.match(r"(?i)final deadline", l) and l not in named and not re.search(r"\bdue\b", l, re.I):
                        out.append((l, n))
            return out

        def _status(n):
            gap = (today - (start + timedelta(days=7 * n - 1))).days
            if gap == 0:
                return "DUE TODAY"
            return f"due in {-gap} days" if gap < 0 else f"was due {gap} days ago"

        rows = []
        for n, text in sorted(_schedule_weeks_for(course_path).items()):
            for name, due_week in _items(n, text):
                d = start + timedelta(days=7 * due_week - 1)
                rows.append((due_week, f"- {name}: due Sunday {d:%b %d} (Week {due_week}, 11:59 pm), {_status(due_week)}"))
        rows = [r for _, r in sorted(rows, key=lambda x: x[0])]
        index = (
            "ALL DUE DATES, resolved to calendar dates in code (use these exactly; do not re-derive them from the "
            "schedule wording, which does not say which Sunday is meant):\n" + "\n".join(rows) + "\n\n"
        ) if rows else ""
        w_last = start + timedelta(days=7 * (TERM_WEEKS - 1))
        index += (
            f"Deadlines the course materials give as weekdays of the last week: \"Monday of Week {TERM_WEEKS}\" is "
            f"{w_last:%A, %B %d, %Y}; \"Friday of Week {TERM_WEEKS}\" (the Final Exam due date, finals week) is "
            f"{w_last + timedelta(days=4):%A, %B %d, %Y}.\n\n"
        )
        def _rel(n):
            return {-1: "LAST WEEK", 0: "THIS WEEK", 1: "NEXT WEEK"}.get(n - week, "")

        full = "\n\n".join(
            f"=== Week {n}{' (' + _rel(n) + ')' if _rel(n) else ''}: "
            f"{start + timedelta(days=7 * (n - 1)):%b %d} to {start + timedelta(days=7 * n - 1):%b %d} ===\n{text}"
            for n, text in sorted(_schedule_weeks_for(course_path).items())
        )
        index += (
            "FULL COURSE SCHEDULE BY WEEK (topics, required reading, required videos, and assignments for every week, "
            "copied from the course schedule). Use it for any question about what a given week covers, including "
            "\"this week\", \"next week\" and \"last week\". When describing a week, include its topics, required "
            "reading, required videos, and assignments, not just the assignments. Weeks run Monday to Sunday.\n"
            f"{full}\n\n"
        ) if full else ""
        due_now = [r for r in rows if "DUE TODAY" in r]
        this_week = (
            f"This is Week {week}, ending Sunday {sunday:%B %d}. "
            "When a student asks what is due today, tonight, tomorrow, or this week, answer from the ALL DUE DATES "
            "list above using the status shown on each line, and list EVERY matching line. An item whose line says "
            "\"due in N days\" or \"was due N days ago\" is NOT due today, even if the schedule says \"due Sunday\". "
            "When asked when a named item is due, give the date on its line. Write the answer naturally; do not copy the list formatting or the words DUE TODAY.\n"
            + ("Items marked DUE TODAY right now:\n" + "\n".join(due_now) + "\n\n" if due_now else "No item is marked DUE TODAY right now.\n\n")
        )
        term_info = (
            f"Week 1 of the term began {start:%A, %B %d, %Y}, so today falls in Week {week}. "
            f"If that is beyond Week {TERM_WEEKS}, the term is over.\n"
            f"{index}{this_week}"
            "Calendar dates for each week (Monday to Sunday):\n"
            f"{weeks}\n"
            "The schedule lists work under a week heading with a weekday, for example \"due Sunday\" under Week 2. "
            "That means the Sunday that ends that week in the table above. When a student asks whether something is "
            "due, late, or past a deadline, find its due date from this table, apply the late policy in the syllabus "
            "to get the cutoff, and compare using the \"days ago\" figure in the table: the late window is still open only if that "
            "figure is no larger than the window length (for a 5-day window, 5 days ago is still open and 6 days ago "
            "is closed). State the dates you used. If the materials "
            "give conflicting late-policy details, say so and point the student to the instructor."
        )
    return {"today": f"{today:%A, %B %d, %Y}", "term_info": term_info}


def _build_export(fmt, course, include_questions=True, include_definitions=True, include_transcript=False, source="sidebar"):
    """Generate a study package from the current chat and store the finished file in session_state."""
    history = st.session_state["chat_history"]
    try:
        with st.spinner("Building your study package..."):
            package = generate_study_package(history, course, api_key=api_key)
            transcript = format_transcript(history) if include_transcript else None
            build = create_study_worksheet if fmt == "PDF" else create_practice_deck
            buffer = build(package, course, include_questions, include_definitions, transcript)
    except Exception as e:
        st.session_state.pop("export", None)
        st.error(f"Could not build the study package: {e}")
        return False
    ext, mime = EXPORT_FORMATS[fmt]
    slug = re.sub(r"[^A-Za-z0-9]+", "_", course).strip("_").lower() or "course"
    st.session_state["export"] = {
        "bytes": buffer.getvalue(),
        "filename": f"{slug}_study_package.{ext}",
        "mime": mime,
        "source": source,
    }
    return True


def _download_export(key):
    export = st.session_state.get("export")
    if export:
        st.download_button(
            "Download " + export["filename"], data=export["bytes"], file_name=export["filename"],
            mime=export["mime"], key=key,
        )


st.set_page_config(page_title="Socratica | Interactive Course Companion", layout="wide")

if sys.version_info[:2] != (3, 12):
    st.error(f"Socratica is pinned to Python 3.12 (running {sys.version.split()[0]}). Use the 3.12 virtual environment or set Python 3.12 in the deployment settings.")
    st.stop()

st.title("Socratica")
st.caption("Interactive Course Companion")

# =============================================================================
# Sidebar Section
# =============================================================================
with st.sidebar:
    st.title("Socratica")
    st.caption("Interactive Course Companion")
    
    # -------------------------------------------------------------------------
    # 1. Course Selection
    # -------------------------------------------------------------------------
    BASE_DATA_DIR = "./data"
    if os.path.exists(BASE_DATA_DIR):
        available_courses = sorted(
            d for d in os.listdir(BASE_DATA_DIR)
            if os.path.isdir(os.path.join(BASE_DATA_DIR, d)) and d not in HIDDEN_COURSES
        )
    else:
        available_courses = []

    if available_courses:
        selected_course = st.sidebar.selectbox("Select Course", available_courses, format_func=_pretty)
        course_path = os.path.join(BASE_DATA_DIR, selected_course)
    else:
        selected_course = None
        st.sidebar.warning("No course materials found. Add course folders under ./data (they are intentionally not stored in the repository).")
        course_path = BASE_DATA_DIR

    # Reset chat history if user switches courses
    if (
        "current_course" not in st.session_state 
        or st.session_state["current_course"] != selected_course
    ):
        st.session_state["current_course"] = selected_course
        st.session_state["chat_history"] = []
        st.session_state["chat_sources"] = {}
        st.session_state["export_offers"] = set()
        st.session_state.pop("export", None)

    # -------------------------------------------------------------------------
    # Writing Feedback (draft upload)
    # -------------------------------------------------------------------------
    st.markdown("---")
    st.subheader("Writing Feedback")
    draft_source = st.radio("Draft source", ["Upload file", "Paste text"], horizontal=True, key="draft_source")
    draft_file = draft_pasted = None
    if draft_source == "Upload file":
        draft_file = st.file_uploader("Upload a draft (.docx, .pdf or .txt)", type=SUPPORTED_TYPES, key="draft_file")
    else:
        draft_pasted = st.text_area("Paste your draft here", height=200, key="draft_pasted")
    draft_assignment = st.text_input("Assignment (optional)", key="draft_assignment")
    has_draft = draft_file is not None if draft_source == "Upload file" else bool((draft_pasted or "").strip())
    if st.button("Get Writing Feedback", disabled=not has_draft or not selected_course, key="draft_go"):
        try:
            with st.spinner("Reading your draft..."):
                draft_text = extract_draft_text(draft_file) if draft_file is not None else clean_draft_text(draft_pasted)
                feedback = review_draft(
                    draft_text, _pretty(selected_course), load_style_guide(course_path),
                    assignment=draft_assignment.strip() or None, api_key=api_key,
                )
            note = f"I submitted a draft for writing feedback: {draft_file.name if draft_file is not None else 'pasted text'}"
            if draft_assignment.strip():
                note += f" ({draft_assignment.strip()})"
            st.session_state["chat_history"].append(HumanMessage(content=note))
            st.session_state["chat_history"].append(AIMessage(content=feedback))
            st.rerun()
        except DraftError as e:
            st.warning(str(e))
        except Exception as e:
            st.error(f"Could not review the draft: {e}")

    # -------------------------------------------------------------------------
    # 2. Study & Review Exports
    # -------------------------------------------------------------------------
    st.markdown("---")
    st.subheader("Study & Review Exports")
    has_chat = bool(st.session_state["chat_history"])

    export_format = st.radio("Format", ["PDF", "PPTX"], horizontal=True, key="export_format")
    include_questions = st.checkbox("Include Practice Questions", value=True, key="opt_questions")
    include_definitions = st.checkbox("Include Concept Definitions", value=True, key="opt_definitions")
    include_transcript = st.checkbox("Include Session Transcript", value=False, key="opt_transcript")

    if not has_chat:
        st.caption("Chat with Socratica first, then generate a study package from the conversation.")
    if st.button("Generate Study Package", disabled=not has_chat, key="gen_sidebar"):
        _build_export(
            export_format, _pretty(selected_course) if selected_course else "Course",
            include_questions, include_definitions, include_transcript,
        )
    _download_export("dl_sidebar")

def _is_answer_key(file_path):
    """True for anything inside an 'answer keys' folder (any case; spaces, underscores or hyphens)
    or whose filename contains 'answer key'. These must never be indexed or retrievable."""
    def norm(s):
        return re.sub(r"[\s_\-]+", " ", s).strip().lower()

    parts = [norm(p) for p in os.path.normpath(file_path).split(os.sep)]
    folders, filename = parts[:-1], parts[-1]
    return any(p in ("answer key", "answer keys") for p in folders) or "answer key" in filename


def _source_label(source, course_path):
    """Human-readable location, e.g. 'BA 336 > Excel Textbook > Chapter 1 > 1.5 Chapter Practice'."""
    rel = os.path.splitext(os.path.relpath(source, course_path))[0] if source else "unknown"
    return " > ".join(_pretty(p) for p in [os.path.basename(course_path)] + rel.split(os.sep))


def _format_sources(docs):
    """Unique, ordered source labels (with PDF page numbers) for the retrieved chunks."""
    seen = []
    for doc in docs:
        label = doc.metadata.get("label") or doc.metadata.get("source", "unknown")
        if isinstance(doc.metadata.get("page"), int):
            label += f" (p. {doc.metadata['page'] + 1})"
        if label not in seen:
            seen.append(label)
    return seen


def _show_sources(sources):
    with st.expander("Retrieved sources"):
        for s in sources:
            st.markdown(f"- {s}")


def _course_fingerprint(path):
    """Hash of file paths, sizes and mtimes (plus INDEX_VERSION) so the persisted index rebuilds when course files or indexing logic change."""
    h = hashlib.sha1(INDEX_VERSION.encode())
    for root, _, files in sorted(os.walk(path)):
        for file in sorted(files):
            fp = os.path.join(root, file)
            stat = os.stat(fp)
            h.update(f"{fp}|{stat.st_size}|{int(stat.st_mtime)}".encode())
    return h.hexdigest()[:10]


# 2. Document Loader
@st.cache_resource
def load_docs(path):
    if not os.path.exists(path) or not os.listdir(path):
        return None

    # Use course name in collection name so Chroma creates isolated collections
    collection_name = os.path.basename(path).replace(" ", "_").lower()
    persist_dir = os.path.join(CHROMA_DIR, f"{collection_name}_{_course_fingerprint(path)}")
    embeddings = OpenAIEmbeddings(openai_api_key=api_key)

    if os.path.isdir(persist_dir):
        vectorstore = Chroma(
            collection_name=collection_name,
            embedding_function=embeddings,
            persist_directory=persist_dir,
        )
        return vectorstore.as_retriever(search_kwargs={"k": RETRIEVER_K})

    documents = []

    # Load PDFs
    pdf_loader = DirectoryLoader(path, glob="**/*.pdf", loader_cls=PyPDFLoader, recursive=True)
    documents.extend(pdf_loader.load())

    # Load DOCX
    docx_loader = DirectoryLoader(path, glob="**/*.docx", loader_cls=Docx2txtLoader, recursive=True)
    documents.extend(docx_loader.load())

    # Load TXT
    txt_loader = DirectoryLoader(path, glob="**/*.txt", loader_cls=TextLoader, recursive=True)
    documents.extend(txt_loader.load())

    # Index spreadsheets as a short summary (columns + sample rows), not every row:
    # thousands of raw rows would flood retrieval with numeric noise.
    for root, _, files in os.walk(path):
        for file in files:
            if file.endswith(".xlsx") and not _is_answer_key(os.path.join(root, file)):
                file_path = os.path.join(root, file)
                try:
                    excel_file = pd.ExcelFile(file_path)
                    for sheet_name in excel_file.sheet_names:
                        df = pd.read_excel(file_path, sheet_name=sheet_name)
                        if df.empty:
                            continue
                        columns = [str(c) for c in df.columns if not str(c).startswith("Unnamed")]
                        sample = df.head(SHEET_SAMPLE_ROWS).fillna("").to_csv(sep="\t", index=False)
                        documents.append(
                            Document(
                                page_content=(
                                    f"DATA FILE SUMMARY: Workbook '{file}', sheet '{sheet_name}' "
                                    f"has {len(df)} rows and {df.shape[1]} columns.\n"
                                    f"Columns: {', '.join(columns)}\n\n"
                                    f"First {SHEET_SAMPLE_ROWS} rows (tab-delimited):\n{sample}"
                                ),
                                metadata={"source": file_path, "sheet": sheet_name},
                            )
                        )
                except Exception as e:
                    st.warning(f"Failed to read XLSX file {file}: {e}")

    # Load PPTX (Slide Decks) using python-pptx directly
    for root, _, files in os.walk(path):
        for file in files:
            if file.endswith(".pptx") and not _is_answer_key(os.path.join(root, file)):
                file_path = os.path.join(root, file)
                try:
                    prs = pptx.Presentation(file_path)
                    text_runs = []
                    for slide in prs.slides:
                        for shape in slide.shapes:
                            if shape.has_text_frame:
                                text_runs.append(shape.text)
                    full_text = "\n".join(text_runs)
                    if full_text.strip():
                        documents.append(Document(page_content=full_text, metadata={"source": file_path}))
                except Exception as e:
                    st.warning(f"Failed to read PPTX file {file}: {e}")

    # Single choke point: whatever the loaders picked up, answer keys never get indexed.
    documents = [
        d for d in documents
        if not _is_answer_key(d.metadata.get("source", ""))
        and os.path.basename(d.metadata.get("source", "")) != TERM_START_FILE
    ]

    if not documents:
        return None

    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)
    splits = text_splitter.split_documents(documents)

    # Prefix every chunk with its course location so "Chapter 1" or "assignment" in a
    # question can match the right folder, and so the LLM knows where each excerpt came from.
    for split in splits:
        label = _source_label(split.metadata.get("source", ""), path)
        split.metadata["label"] = label
        split.page_content = f"[Source: {label}]\n{split.page_content}"

    try:
        vectorstore = Chroma.from_documents(
            documents=splits,
            embedding=embeddings,
            collection_name=collection_name,
            persist_directory=persist_dir,
        )
    except Exception:
        shutil.rmtree(persist_dir, ignore_errors=True)
        raise
    return vectorstore.as_retriever(search_kwargs={"k": RETRIEVER_K})

retriever = load_docs(course_path)

if retriever is not None:
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, openai_api_key=api_key)

    # 3. Contextualize Question Prompt (Solves the "Follow-up question" issue)
    contextualize_q_system_prompt = (
        "Given a chat history and the latest user question "
        "which might reference context in the chat history, "
        "formulate a standalone question which can be understood "
        "without the chat history. If the latest question clearly changes "
        "topic (for example \"moving to another question\"), do not carry "
        "over topics, files, or assignments from earlier in the conversation. "
        "Do NOT answer the question, "
        "just reformulate it if needed and otherwise return it as is."
    )
    contextualize_q_prompt = ChatPromptTemplate.from_messages([
        ("system", contextualize_q_system_prompt),
        MessagesPlaceholder("chat_history"),
        ("human", "{input}"),
    ])
    history_aware_retriever = create_history_aware_retriever(llm, retriever, contextualize_q_prompt)

    # 4. Q&A Prompt (Solves the "Confusing assignments" issue)
    system_prompt = (
        """You are Socratica, a patient, encouraging academic mentor for college students. 
Your primary goal is to guide students to understand concepts and work through homework problems on their own using the Socratic method.

CORE RULES:
1. DON'T HAND OVER ANSWERS BEFORE THE STUDENT HAS TRIED: Do not solve math problems, write code, produce essay paragraphs, or give direct answers to homework/quiz questions that the student has not yet worked on themselves. Never write work the student could submit as their own.
2. ASK GUIDING QUESTIONS: While the student is still working toward an idea, ask ONE clear, thought-provoking question at a time to help them identify the next step, underlying concept, or logical error.
3. SCAFFOLD LEARNING: If a student is stuck or confused, offer a brief hint, a simple real-world analogy, or point them toward relevant concepts, then prompt them to try again.
4. VALIDATE EFFORT: Acknowledge student attempts with warm, encouraging feedback before moving on.
5. KEEP IT ACCESSIBLE: Break down academic jargon and maintain a supportive, approachable, and conversational tone.

REINFORCE UNDERSTANDING ONCE IT IS DEMONSTRATED:
A student has demonstrated understanding when they explain an idea correctly in their own words, apply it correctly to a new example, or reason their way to a correct answer. Saying "I get it", guessing, or answering a yes/no question does not count; if you are unsure, ask them to explain their reasoning first.
When they have demonstrated understanding:
- Confirm specifically what they got right and why it works, so the idea is named and anchored.
- Restate the idea in a different way than they did, using one or two approaches such as a real-world analogy, a step-by-step walkthrough, a short example, a description of a simple diagram or visual, or a one-sentence summary. Vary the approach across the conversation to reach different learning styles, and occasionally ask which kind of explanation helps them most.
- Connect it to the bigger picture of the course (for example, a related concept or where it shows up later).
- End with one short check question or a slightly harder variation so they can apply the idea in a new setting.
If their understanding is only partly correct, affirm the correct part and ask a guiding question about the gap instead of explaining the whole answer. Reinforcing what the student has shown they know is not the same as giving away an answer they haven't reached; never reveal an answer key or solution to work the student has not yet attempted.

EXPLAINING COURSE CONTENT IS ALLOWED:
The Socratic rules above apply to graded or assigned work (homework, quizzes, projects, and textbook practice or scored exercises). They do NOT apply to teaching content. When a student asks for a summary, overview, or explanation of a textbook chapter, section, slide deck, or concept, give it directly: a short high-level overview, then the key ideas and the important functions, formulas, or terms the material covers, each with a one-line description of what it is for and its syntax or general form. Never refuse or deflect these requests. Base the overview only on the excerpts below; if they clearly cover only some sections of what was asked, summarize those, name which sections you covered, and offer to go deeper on the rest. After the overview, end with one short question that invites the student to try applying an idea. Do not use this to solve a specific assignment or practice problem for them.

TODAY'S DATE AND THE COURSE CALENDAR:
Today's date is {today}. {term_info}

COURSE MATERIALS:
Use the course materials below to ground your questions, hints, and explanations in what this course actually teaches. For assigned work, treat them as background for guiding the student rather than a script to read answers from.
Each excerpt starts with a "[Source: ...]" line showing where it comes from in the course (for example a textbook chapter, an assignment, or a rubric). Use it to tell these apart, and do not mix up a textbook practice exercise with a separate course assignment.

FORMATTING:
Write in plain text and simple Markdown (bold, lists, and `code` for Excel formulas). Never use LaTeX or math markup such as \\[ \\], \\( \\), $$, \\frac, or \\text; it does not render here. Write formulas the way a student would type them in Excel, for example `=0.06/12` or `=PMT(rate, nper, pv)`, and put them in backticks.

OFFERING A STUDY PACKAGE:
Only when you have just reinforced a concept the student has demonstrated understanding of (see above), and you have not already offered for this topic, end your reply with a final line containing exactly [[OFFER_STUDY_PACKAGE]] and nothing else. Never mention or explain this marker, and never include it otherwise.

BE HONEST ABOUT WHAT YOU KNOW:
- Never say or imply that the student told you something they did not say. The course materials come from the course itself, not from the student, so never say they "provided" or "uploaded" a schedule or any file; call it "the course schedule" or "the syllabus". Only refer to what they actually wrote in this conversation.
- Only mention files, assignments, or data sets that appear in the course materials below. Do not guess or invent them.
- If the materials do not clearly answer the question, or the question could refer to more than one item (for example both a textbook practice exercise and a course assignment), say briefly what you found, name the options, and ask the student which one they mean before going further.

{context}"""
    )
    
    qa_prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        MessagesPlaceholder("chat_history"),
        ("human", "{input}"),
    ])

    question_answer_chain = create_stuff_documents_chain(llm, qa_prompt)
    rag_chain = create_retrieval_chain(history_aware_retriever, question_answer_chain)

    # 5. Display Chat History
    chat_sources = st.session_state.setdefault("chat_sources", {})
    export_offers = st.session_state.setdefault("export_offers", set())
    course_label = _pretty(selected_course) if selected_course else "Course"
    for i, message in enumerate(st.session_state["chat_history"]):
        if isinstance(message, HumanMessage):
            with st.chat_message("user"):
                st.write(message.content)
        elif isinstance(message, AIMessage):
            with st.chat_message("assistant"):
                st.write(message.content)
                if chat_sources.get(i):
                    _show_sources(chat_sources[i])
                if i in export_offers:
                    st.caption("Nice work. Want a study package built from this conversation?")
                    col_deck, col_pdf = st.columns(2)
                    if col_deck.button("Generate Deck", key=f"offer_deck_{i}"):
                        _build_export("PPTX", course_label, source=i)
                    if col_pdf.button("Generate PDF", key=f"offer_pdf_{i}"):
                        _build_export("PDF", course_label, source=i)
                    if st.session_state.get("export", {}).get("source") == i:
                        _download_export(f"dl_offer_{i}")

    # 6. User Chat Input
    if user_input := st.chat_input("Ask a question about this course..."):
        with st.chat_message("user"):
            st.write(user_input)

        with st.spinner("Thinking..."):
            response = rag_chain.invoke({
                "input": user_input,
                "chat_history": st.session_state["chat_history"],
                **_calendar_context(course_path),
            })
        sources = _format_sources(response.get("context", []))
        answer = response["answer"]
        offered = OFFER_TOKEN in answer
        answer = answer.replace(OFFER_TOKEN, "").strip()

        # Update memory, then rerun so the new turn renders through the history loop above
        st.session_state["chat_history"].append(HumanMessage(content=user_input))
        st.session_state["chat_history"].append(AIMessage(content=answer))
        ai_index = len(st.session_state["chat_history"]) - 1
        chat_sources[ai_index] = sources
        if offered:
            export_offers.add(ai_index)
        st.rerun()
