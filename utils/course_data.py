"""Fetch private course materials into ./data at startup.

The public repository ships only a demo course. A deployment that should serve real
course files sets these secrets (Streamlit secrets or environment variables):

  COURSE_DATA_REPO   "owner/name" of the private repository that holds a top-level data/ folder
  COURSE_DATA_TOKEN  read-only GitHub token for that repository (fine-grained, Contents: read)
  COURSE_DATA_REF    optional branch, tag or commit (default "main")

With no COURSE_DATA_REPO/COURSE_DATA_TOKEN set (local development, the demo), nothing happens.
"""
import os
import shutil
import tarfile
import tempfile

import requests
import streamlit as st

DATA_DIR = "./data"
_PREFIX = "data"
_TIMEOUT = (10, 120)


def _secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:  # no secrets.toml when running locally
        pass
    return os.getenv(name)


def _extract_data_folder(tar, dest):
    """Extract regular files under the archive's top-level data/ folder into dest."""
    for member in tar:
        if not member.isfile():
            continue
        parts = member.name.split("/")[1:]  # drop the "<owner>-<repo>-<sha>" root GitHub adds
        if len(parts) < 2 or parts[0] != _PREFIX or ".." in parts:
            continue
        target = os.path.join(dest, *parts[1:])
        if os.path.commonpath([os.path.abspath(dest), os.path.abspath(target)]) != os.path.abspath(dest):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with tar.extractfile(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        os.utime(target, (member.mtime, member.mtime))


@st.cache_resource(show_spinner="Loading course materials...")
def _download(repo, ref, token):
    url = f"https://api.github.com/repos/{repo}/tarball/{ref}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    with tempfile.TemporaryDirectory() as tmp:
        with requests.get(url, headers=headers, stream=True, timeout=_TIMEOUT) as resp:
            resp.raise_for_status()
            with tarfile.open(fileobj=resp.raw, mode="r|gz") as tar:
                _extract_data_folder(tar, tmp)
        if not os.listdir(tmp):
            raise OSError("the repository has no data/ folder")
        shutil.copytree(tmp, DATA_DIR, dirs_exist_ok=True)
    return True


def ensure_course_data():
    """Download the private course folders into ./data once per server process.

    Returns True when remote materials are in place, False when none are configured or the
    download failed (the app then shows whatever is already in ./data).
    """
    repo = _secret("COURSE_DATA_REPO")
    token = _secret("COURSE_DATA_TOKEN")
    if not repo or not token:
        return False
    try:
        return _download(repo, _secret("COURSE_DATA_REF") or "main", token)
    except (requests.RequestException, tarfile.TarError, OSError) as exc:
        st.error(f"Could not download course materials ({type(exc).__name__}). Check the COURSE_DATA_* secrets.")
        return False
