import os

import streamlit as st
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

# One entry per job the app gives a model. Today every role runs on OpenAI.
ROLES = {
    "tutor": {"model": "gpt-4o-mini", "temperature": 0},
    "writing_feedback": {"model": "gpt-4o", "temperature": 0.2},
}


def get_api_key():
    """Streamlit Cloud secret first, then the local .env / environment variable."""
    try:
        if "OPENAI_API_KEY" in st.secrets:
            return st.secrets["OPENAI_API_KEY"]
    except Exception:  # no secrets.toml when running locally
        pass
    return os.getenv("OPENAI_API_KEY")


def get_chat_model(role):
    settings = ROLES[role]
    return ChatOpenAI(model=settings["model"], temperature=settings["temperature"], openai_api_key=get_api_key())


def get_embeddings():
    return OpenAIEmbeddings(openai_api_key=get_api_key())
