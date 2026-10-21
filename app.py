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
from langchain.chains import create_history_aware_retriever, create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.documents import Document
from utils.instructions import build_tutor_system_prompt
from utils.llm import get_chat_model, get_embeddings
from utils.writing_tutor import DraftError, MAX_DRAFT_CHARS, SUPPORTED_TYPES, clean_draft_text, extract_draft_text, list_assignments, load_assignment_context, load_style_guide, review_draft

load_dotenv()
CHROMA_DIR = "./chroma"
HIDDEN_COURSES = {"ba_101"}  # parked course folders: kept in ./data but not shown in the UI
RETRIEVER_K = 10  # chunks per question; high enough to cover most of a textbook chapter for overviews
INDEX_VERSION = "3"  # bump whenever ingestion/chunking changes so cached indexes rebuild
SHEET_SAMPLE_ROWS = 5
APP_TIMEZONE = "America/New_York"
TERM_START_FILE = "term_start.txt"  # one line per course folder: the first day of Week 1, as YYYY-MM-DD
TERM_WEEKS = 11


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

    # -------------------------------------------------------------------------
    # Writing Feedback (draft upload)
    # -------------------------------------------------------------------------
    st.markdown("---")
    st.subheader("Writing Feedback")
    st.caption(
        "Upload your draft to get feedback: what is working, what is not working, and questions to guide your revision. "
        "Your draft will get checked against the department style guide and the assignment rubric. "
        "It does not rewrite your draft or grade it. "
        "Paste text or upload your file first, then click 'Get Feedback' to see the review. You can also paste your draft text directly."
    )
    draft_source = st.radio("Draft source", ["Upload file", "Paste text"], horizontal=True, key="draft_source")
    draft_file = draft_pasted = None
    if draft_source == "Upload file":
        draft_file = st.file_uploader("Upload a draft (.docx, .pdf or .txt)", type=SUPPORTED_TYPES, key="draft_file")
    else:
        draft_pasted = st.text_area("Paste your draft here", height=200, key="draft_pasted")
        st.caption(f"Drafts up to {MAX_DRAFT_CHARS:,} characters.")
    assignment_folders = list_assignments(course_path) if selected_course else []
    OTHER = "Other / Not Listed"
    assignment_choice = st.selectbox(
        "Which assignment is this for?", [OTHER] + assignment_folders,
        format_func=lambda x: x if x == OTHER else _pretty(x), key="draft_assignment_choice",
        help="Pick the assignment so the review can check your draft against its instructions and rubric.",
    )
    if assignment_choice == OTHER:
        draft_assignment = ""
        assignment_context = None
    else:
        draft_assignment = _pretty(assignment_choice)
        assignment_context = load_assignment_context(course_path, assignment_choice)
    has_draft = draft_file is not None if draft_source == "Upload file" else bool((draft_pasted or "").strip())
    if st.button("Get Feedback", disabled=not has_draft or not selected_course, key="draft_go"):
        try:
            with st.spinner("Reading your draft..."):
                draft_text = extract_draft_text(draft_file) if draft_file is not None else clean_draft_text(draft_pasted)
                feedback = review_draft(
                    draft_text, _pretty(selected_course), load_style_guide(course_path),
                    assignment=draft_assignment or None, assignment_context=assignment_context,
                )
            note = f"I submitted a draft for writing feedback: {draft_file.name if draft_file is not None else 'pasted text'}"
            if draft_assignment:
                note += f" ({draft_assignment})"
            st.session_state["chat_history"].append(HumanMessage(content=note))
            st.session_state["chat_history"].append(AIMessage(content=feedback))
            st.rerun()
        except DraftError as e:
            st.warning(str(e))
        except Exception as e:
            st.error(f"Could not review the draft: {e}")


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
    embeddings = get_embeddings()

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
    llm = get_chat_model("tutor")

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
    system_prompt = build_tutor_system_prompt()
    
    qa_prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        MessagesPlaceholder("chat_history"),
        ("human", "{input}"),
    ])

    question_answer_chain = create_stuff_documents_chain(llm, qa_prompt)
    rag_chain = create_retrieval_chain(history_aware_retriever, question_answer_chain)

    # 5. Display Chat History
    chat_sources = st.session_state.setdefault("chat_sources", {})
    for i, message in enumerate(st.session_state["chat_history"]):
        if isinstance(message, HumanMessage):
            with st.chat_message("user"):
                st.write(message.content)
        elif isinstance(message, AIMessage):
            with st.chat_message("assistant"):
                st.write(message.content)
                if chat_sources.get(i):
                    _show_sources(chat_sources[i])

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
        answer = answer.strip()

        # Update memory, then rerun so the new turn renders through the history loop above
        st.session_state["chat_history"].append(HumanMessage(content=user_input))
        st.session_state["chat_history"].append(AIMessage(content=answer))
        ai_index = len(st.session_state["chat_history"]) - 1
        chat_sources[ai_index] = sources
        st.rerun()
