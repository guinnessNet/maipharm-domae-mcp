import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_public_data.py"
LOGIN_ID = "shop" + "_user.01"           # 시험 전용 가상 아이디(밑줄·마침표 포함)
PASSWORD = "Pw!" + "x9#long-secret"      # 시험 전용 가상 비밀번호(특수문자, 12자 초과)
SPACED_ID = "abc" + " pharm7"            # 시험 전용 가상 아이디(중간 띄어쓰기)


def run(tmp_path, text, deny=(LOGIN_ID, PASSWORD), deny_raw=None, set_env=True, deny_bytes=None):
    f = tmp_path / "sample.py"
    f.write_text(text, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PUBLIC_DATA_DENYLIST"}
    if set_env:
        d = tmp_path / "denylist"
        if deny_bytes is not None:
            d.write_bytes(deny_bytes)
        else:
            d.write_text(deny_raw if deny_raw is not None else "".join(v + "\n" for v in deny), encoding="utf-8")
        env["PUBLIC_DATA_DENYLIST"] = str(d)
    else:
        env["PUBLIC_DATA_DENYLIST"] = str(tmp_path / "does-not-exist")
    return subprocess.run([sys.executable, str(SCRIPT), str(f)], capture_output=True, text=True, env=env)


def test_clean_file_passes(tmp_path):
    r = run(tmp_path, "code = '699000001'\nuser = 'TEST0001'\nchat_id = '1000000001'\n")
    assert r.returncode == 0, r.stdout


def test_denylisted_login_id_fails_without_echoing_value(tmp_path):
    r = run(tmp_path, f"login = {LOGIN_ID!r}\n")
    assert r.returncode == 1 and "denylisted" in r.stdout
    assert LOGIN_ID not in r.stdout + r.stderr


def test_denylisted_password_with_symbols_fails_anywhere_in_line(tmp_path):
    for text in (f"pw = {PASSWORD!r}\n", f"url = 'https://x/login?pw={PASSWORD}&a=1'\n", f"# {PASSWORD}\n"):
        r = run(tmp_path, text)
        assert r.returncode == 1 and "denylisted" in r.stdout
        assert PASSWORD not in r.stdout + r.stderr


def test_real_like_chat_id_fails(tmp_path):
    chat = "-100" + "987654321"
    r = run(tmp_path, f"chat_id = {chat!r}\n")
    assert r.returncode == 1 and "chat_id" in r.stdout
    assert chat not in r.stdout + r.stderr


def test_missing_denylist_fails_closed(tmp_path):
    r = run(tmp_path, "x = 1\n", set_env=False)
    assert r.returncode == 2 and "config_error" in r.stdout


def test_empty_or_too_short_denylist_fails_closed(tmp_path):
    for raw in ("", "\n\n", "abc\n"):
        r = run(tmp_path, "x = 1\n", deny_raw=raw)
        assert r.returncode == 2 and "config_error" in r.stdout


def test_non_utf8_denylist_fails_closed_as_config_error(tmp_path):
    r = run(tmp_path, "x = 1\n", deny_bytes=b"\xff\xfe\x00broken\n")
    assert r.returncode == 2 and "config_error" in r.stdout


def test_default_denylist_file_is_gitignored():
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(["git", "check-ignore", "-q", ".public-data-denylist"], cwd=root)
    assert r.returncode == 0


def test_spaced_id_matches_with_or_without_space(tmp_path):
    joined = SPACED_ID.replace(" ", "")
    for text in (f"id = {SPACED_ID!r}\n", f"id = {joined!r}\n", f"id = {SPACED_ID.replace(' ', '  ')!r}\n"):
        r = run(tmp_path, text, deny=(SPACED_ID,))
        assert r.returncode == 1 and "denylisted" in r.stdout
        assert SPACED_ID not in r.stdout + r.stderr and joined not in r.stdout + r.stderr
