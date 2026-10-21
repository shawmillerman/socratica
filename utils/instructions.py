from pathlib import Path

INSTRUCTIONS_DIR = Path(__file__).resolve().parent.parent / "instructions"

CALENDAR_SECTION = "TODAY'S DATE AND THE COURSE CALENDAR:\nToday's date is {today}. {term_info}"


def load_instruction(name):
    """Text of instructions/<name>.md with trailing newlines removed."""
    return (INSTRUCTIONS_DIR / f"{name}.md").read_text(encoding="utf-8").rstrip("\n")


def _offer_section(offer_token):
    return (
        "OFFERING A STUDY PACKAGE:\n"
        "Only when you have just reinforced a concept the student has demonstrated understanding of (see above), "
        "and you have not already offered for this topic, end your reply with a final line containing exactly "
        f"{offer_token} and nothing else. Never mention or explain this marker, and never include it otherwise."
    )


def build_tutor_system_prompt(offer_token):
    """Tutor system prompt assembled from instructions/ files plus the code-owned technical sections.

    The result keeps the {today}, {term_info} and {context} placeholders for the prompt template to fill.
    """
    sections = [
        load_instruction("pedagogy/core_rules"),
        load_instruction("pedagogy/reinforcing_understanding"),
        load_instruction("pedagogy/explaining_course_content"),
        CALENDAR_SECTION,
        load_instruction("integrity/using_course_materials"),
        load_instruction("system/formatting"),
        _offer_section(offer_token),
        load_instruction("integrity/honesty"),
        "{context}",
    ]
    return "\n\n".join(sections)
