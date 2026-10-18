import os
import re
from pathlib import Path

import docx2txt
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pypdf import PdfReader

WRITING_MODEL = "gpt-4o"
SUPPORTED_TYPES = ["docx", "pdf", "txt"]
MAX_DRAFT_CHARS = 30000
PROMPT_FILE = Path(__file__).with_name("writing_tutor_prompt.txt")
NO_STYLE_GUIDE = "(No department style guide is available for this course. Give general writing feedback only.)"


class DraftError(ValueError):
    """A problem with the uploaded file that the student can act on."""


def _read_file(source, ext):
    """Text from a path or file-like object of type docx, pdf or txt."""
    if ext == "docx":
        return docx2txt.process(source)
    if ext == "pdf":
        return "\n".join(page.extract_text() or "" for page in PdfReader(source).pages)
    if hasattr(source, "read"):
        return source.read().decode("utf-8", errors="ignore")
    with open(source, encoding="utf-8", errors="ignore") as f:
        return f.read()


def extract_draft_text(uploaded_file):
    """Text of a Streamlit UploadedFile; raises DraftError with a student-friendly message."""
    ext = os.path.splitext(uploaded_file.name)[1].lower().lstrip(".")
    if ext not in SUPPORTED_TYPES:
        raise DraftError("Please upload a .docx, .pdf or .txt file.")
    try:
        text = _read_file(uploaded_file, ext)
    except Exception:
        raise DraftError("I couldn't read that file. Try saving it again as .docx or .pdf.")
    return clean_draft_text(text)


def clean_draft_text(text):
    """Normalize and validate draft text (from a file or pasted); raises DraftError if unusable."""
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    if len(text) < 50:
        raise DraftError("I couldn't find enough text to review. Scanned pages and images can't be read.")
    if len(text) > MAX_DRAFT_CHARS:
        raise DraftError(f"That draft is too long to review in one go (limit about {MAX_DRAFT_CHARS:,} characters).")
    return text


def load_style_guide(course_path):
    """Text of any file under the course folder whose name contains 'style guide' (any spacing or case)."""
    parts = []
    for root, _dirs, files in os.walk(course_path):
        for name in sorted(files):
            stem, ext = os.path.splitext(name)
            if "style guide" not in re.sub(r"[\s_\-]+", " ", stem).lower() or ext.lower().lstrip(".") not in SUPPORTED_TYPES:
                continue
            try:
                parts.append(_read_file(os.path.join(root, name), ext.lower().lstrip(".")).strip())
            except Exception:
                continue
    return "\n\n".join(p for p in parts if p)


def review_draft(draft_text, course_name, style_guide, assignment=None, api_key=None):
    """Writing feedback (markdown) for a student's draft."""
    system = PROMPT_FILE.read_text(encoding="utf-8").replace("{course}", course_name)
    if "{style_guide}" in system:
        system = system.replace("{style_guide}", style_guide or NO_STYLE_GUIDE)
    elif style_guide:
        system += f"\n\nADDITIONAL STYLE GUIDE FILE FROM THE COURSE FOLDER\n{style_guide}"
    label = f"Assignment: {assignment}\n\n" if assignment else ""
    llm = ChatOpenAI(model=WRITING_MODEL, temperature=0.2, openai_api_key=api_key)
    reply = llm.invoke([
        SystemMessage(content=system),
        HumanMessage(content=f"{label}STUDENT DRAFT (review this text only):\n\n{draft_text}"),
    ])
    return reply.content.strip()
