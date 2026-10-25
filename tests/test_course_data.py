import io
import tarfile

import pytest
import requests

from utils import course_data


def _tarball(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = 1_700_000_000
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class _Resp:
    def __init__(self, body, status=200):
        self.raw = io.BytesIO(body)
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(course_data, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("COURSE_DATA_REPO", raising=False)
    monkeypatch.delenv("COURSE_DATA_TOKEN", raising=False)
    monkeypatch.delenv("COURSE_DATA_REF", raising=False)
    course_data._download.clear()
    return tmp_path


def test_noop_without_secrets(isolated, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not hit the network")

    monkeypatch.setattr(course_data.requests, "get", boom)
    assert course_data.ensure_course_data() is False
    assert not (isolated / "data").exists()


def test_downloads_only_data_folder(isolated, monkeypatch):
    body = _tarball({
        "owner-repo-abc123/app.py": b"code",
        "owner-repo-abc123/data/ba_336/syllabus.txt": b"week 1",
        "owner-repo-abc123/data/ba_336/assignments/a.txt": b"hello",
        "owner-repo-abc123/instructions/x.md": b"nope",
    })
    seen = {}

    def fake_get(url, headers, stream, timeout):
        seen.update(url=url, headers=headers)
        return _Resp(body)

    monkeypatch.setattr(course_data.requests, "get", fake_get)
    monkeypatch.setenv("COURSE_DATA_REPO", "owner/repo")
    monkeypatch.setenv("COURSE_DATA_TOKEN", "tok")

    assert course_data.ensure_course_data() is True
    data = isolated / "data"
    assert (data / "ba_336" / "syllabus.txt").read_bytes() == b"week 1"
    assert (data / "ba_336" / "assignments" / "a.txt").read_bytes() == b"hello"
    assert not (data / "app.py").exists()
    assert not (isolated / "instructions").exists()
    assert seen["url"] == "https://api.github.com/repos/owner/repo/tarball/main"
    assert seen["headers"]["Authorization"] == "Bearer tok"


def test_keeps_existing_demo_course(isolated, monkeypatch):
    demo = isolated / "data" / "demo_course"
    demo.mkdir(parents=True)
    (demo / "readme.txt").write_text("demo")
    body = _tarball({"o-r-1/data/ba_336/s.txt": b"x"})
    monkeypatch.setattr(course_data.requests, "get", lambda *a, **k: _Resp(body))
    monkeypatch.setenv("COURSE_DATA_REPO", "o/r")
    monkeypatch.setenv("COURSE_DATA_TOKEN", "t")

    assert course_data.ensure_course_data() is True
    assert (demo / "readme.txt").exists()
    assert (isolated / "data" / "ba_336" / "s.txt").exists()


def test_rejects_path_traversal(isolated, monkeypatch):
    body = _tarball({
        "o-r-1/data/../../evil.txt": b"bad",
        "o-r-1/data/ok/fine.txt": b"good",
    })
    monkeypatch.setattr(course_data.requests, "get", lambda *a, **k: _Resp(body))
    monkeypatch.setenv("COURSE_DATA_REPO", "o/r")
    monkeypatch.setenv("COURSE_DATA_TOKEN", "t")

    assert course_data.ensure_course_data() is True
    assert not (isolated / "evil.txt").exists()
    assert (isolated / "data" / "ok" / "fine.txt").exists()


def test_http_error_returns_false_without_leaking_token(isolated, monkeypatch):
    errors = []
    monkeypatch.setattr(course_data.requests, "get", lambda *a, **k: _Resp(b"", status=404))
    monkeypatch.setattr(course_data.st, "error", errors.append)
    monkeypatch.setenv("COURSE_DATA_REPO", "o/r")
    monkeypatch.setenv("COURSE_DATA_TOKEN", "secret-token")

    assert course_data.ensure_course_data() is False
    assert errors and "secret-token" not in errors[0]
    assert not (isolated / "data").exists()


def test_empty_repo_data_folder_is_an_error(isolated, monkeypatch):
    errors = []
    body = _tarball({"o-r-1/README.md": b"hi"})
    monkeypatch.setattr(course_data.requests, "get", lambda *a, **k: _Resp(body))
    monkeypatch.setattr(course_data.st, "error", errors.append)
    monkeypatch.setenv("COURSE_DATA_REPO", "o/r")
    monkeypatch.setenv("COURSE_DATA_TOKEN", "t")

    assert course_data.ensure_course_data() is False
    assert errors
