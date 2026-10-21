import os
import re
from pathlib import Path

import docx2txt
from langchain_core.messages import HumanMessage, SystemMessage
from pypdf import PdfReader

from utils.instructions import load_instruction
from utils.llm import get_chat_model

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


ASSIGNMENT_DIR = "assignments"
CONTEXT_KEYWORDS = ("instruction", "rubric", "guide")
MAX_CONTEXT_CHARS = 12000


def _norm(name):
    return re.sub(r"[\s_\-]+", " ", name).strip().lower()


def list_assignments(course_path):
    """Sorted folder names under the course's assignments folder, excluding answer keys."""
    root = os.path.join(course_path, ASSIGNMENT_DIR)
    if not os.path.isdir(root):
        return []
    return sorted(
        d for d in os.listdir(root)
        if os.path.isdir(os.path.join(root, d)) and "answer key" not in _norm(d)
    )


def load_assignment_context(course_path, folder):
    """Instructions, rubric and guide text from one assignment folder (answer keys never read)."""
    root = os.path.join(course_path, ASSIGNMENT_DIR, folder)
    parts, used = [], 0
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        stem, ext = os.path.splitext(name)
        ext = ext.lower().lstrip(".")
        key = _norm(stem)
        if ext not in SUPPORTED_TYPES or "answer key" in key or not any(k in key for k in CONTEXT_KEYWORDS):
            continue
        try:
            text = _read_file(os.path.join(root, name), ext).strip()
        except Exception:
            continue
        if ext == "pdf":
            text = " ".join(text.split())
        text = text[: max(0, MAX_CONTEXT_CHARS - used)]
        if text:
            parts.append(f"[{name}]\n{text}")
            used += len(text)
    return "\n\n".join(parts)


def review_draft(draft_text, course_name, style_guide, assignment=None, assignment_context=None):
    """Writing feedback (markdown) for a student's draft."""
    system = PROMPT_FILE.read_text(encoding="utf-8").replace("{course}", course_name).replace("{voice}", load_instruction("voice/tone_and_encouragement"))
    if "{style_guide}" in system:
        system = system.replace("{style_guide}", style_guide or NO_STYLE_GUIDE)
    elif style_guide:
        system += f"\n\nADDITIONAL STYLE GUIDE FILE FROM THE COURSE FOLDER\n{style_guide}"
    if assignment_context:
        system += (
            f"\n\nASSIGNMENT MATERIALS (the instructions, rubric and guide for \"{assignment}\")\n{assignment_context}\n\n"
            "Use these materials only to check the draft's form against what the assignment asks: required sections, "
            "length limits, format, and rubric categories. Mention anything the draft seems to be missing, as a question "
            "or pointer. Never supply content for the assignment and never say whether the draft's analysis is correct."
        )
    label = f"Assignment: {assignment}\n\n" if assignment else ""
    llm = get_chat_model("writing_feedback")
    reply = llm.invoke([
        SystemMessage(content=system),
        HumanMessage(content=f"{label}STUDENT DRAFT (review this text only):\n\n{draft_text}"),
    ])
    return reply.content.strip()
