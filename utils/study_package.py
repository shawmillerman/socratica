import os

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.pydantic_v1 import BaseModel, Field
from langchain_openai import ChatOpenAI

EXPORT_MODEL = "gpt-4o"
MAX_TRANSCRIPT_MESSAGES = 40


class Concept(BaseModel):
    term: str = Field(description="A key term, rule, or formula from the discussion.")
    definition: str = Field(description="A short, plain-language definition.")


class PracticeQuestion(BaseModel):
    question: str = Field(description="A NEW practice question on the same concept.")
    hint: str = Field(description="One Socratic hint that nudges without revealing the answer.")
    guidance_steps: list[str] = Field(
        description="2-4 guiding steps phrased as questions or next-step nudges, not final answers."
    )


class StudyPackage(BaseModel):
    session_summary: str = Field(description="Two sentences on the main concept discussed.")
    concepts: list[Concept]
    pitfalls: list[str] = Field(
        description="Mistakes or misconceptions the student actually hit in this chat, framed constructively."
    )
    practice_questions: list[PracticeQuestion] = Field(description="3-5 fresh practice questions.")


EXTRACTION_PROMPT = """You are Socratica, creating a study package from a tutoring conversation between a student and an AI tutor for the course "{course}".

Build the package ONLY from what was actually discussed. Do not invent topics, facts, or struggles the conversation does not support.

Rules:
- session_summary: exactly two sentences on the main concept discussed.
- concepts: the key terms, rules, or formulas that came up, each with a short plain-language definition.
- pitfalls: mistakes or misconceptions the student really stumbled on in this chat, framed constructively ("Watch out for..."). If there were none, return an empty list.
- practice_questions: 3-5 NEW questions that test the same concept. Never reuse the student's own homework, quiz, or assignment question, and never copy an exercise verbatim from course materials. Vary the scenario so the student must apply the idea, not recall an answer.
- For every practice question, give one hint and 2-4 guidance steps. Hints and steps are scaffolding (guiding questions or next-step nudges). NEVER state the final answer or the full solution.
- Keep the language warm, clear, and free of jargon where possible. Plain text only (no markdown or emoji)."""


def format_transcript(chat_history, limit=MAX_TRANSCRIPT_MESSAGES):
    """Return the most recent messages as (speaker, text) pairs."""
    pairs = []
    for message in chat_history[-limit:]:
        if isinstance(message, HumanMessage):
            pairs.append(("Student", message.content))
        elif isinstance(message, AIMessage):
            pairs.append(("Socratica", message.content))
    return pairs


def generate_study_package(chat_history, course_name, api_key=None):
    """Run a structured extraction over the chat history and return a StudyPackage."""
    transcript = format_transcript(chat_history)
    if not transcript:
        raise ValueError("There is no conversation to summarize yet.")

    llm = ChatOpenAI(
        model=EXPORT_MODEL,
        temperature=0.3,
        openai_api_key=api_key or os.getenv("OPENAI_API_KEY"),
    ).with_structured_output(StudyPackage)

    prompt = ChatPromptTemplate.from_messages([
        ("system", EXTRACTION_PROMPT),
        ("human", "Conversation transcript:\n\n{transcript}"),
    ])
    text = "\n\n".join(f"{speaker}: {content}" for speaker, content in transcript)
    return (prompt | llm).invoke({"course": course_name, "transcript": text})
