from pathlib import Path

INSTRUCTIONS_DIR = Path(__file__).resolve().parent.parent / "instructions"

CALENDAR_SECTION = "TODAY'S DATE AND THE COURSE CALENDAR:\nToday's date is {today}. {term_info}"


def load_instruction(name):
    """Text of instructions/<name>.md with trailing newlines removed."""
    return (INSTRUCTIONS_DIR / f"{name}.md").read_text(encoding="utf-8").rstrip("\n")


def build_tutor_system_prompt():
    """Tutor system prompt assembled from instructions/ files plus the code-owned calendar and context sections.

    The result keeps the {today}, {term_info} and {context} placeholders for the prompt template to fill.
    """
    sections = [
        load_instruction("pedagogy/core_rules"),
        load_instruction("voice/tone_and_encouragement"),
        load_instruction("pedagogy/reinforcing_understanding"),
        load_instruction("pedagogy/explaining_course_content"),
        CALENDAR_SECTION,
        load_instruction("integrity/using_course_materials"),
        load_instruction("system/formatting"),
        load_instruction("integrity/honesty"),
        "{context}",
    ]
    return "\n\n".join(sections)
