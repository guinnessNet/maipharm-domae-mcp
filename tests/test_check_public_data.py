import hashlib
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_public_data.py"
SUPPLIER = "SUP" + "9876"            # 시험 전용 가상 거래처 코드
PHARMACY = "가나" + "다 약국"          # 시험 전용 가상 약국명(공백 포함)


def _digest(value: str) -> str:
    return hashlib.sha256("".join(value.split()).encode("utf-8")).hexdigest()


def run(tmp_path, text, deny=(SUPPLIER, PHARMACY), deny_raw=None, set_env=True):
    f = tmp_path / "sample.py"
    f.write_text(text, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PUBLIC_DATA_DENYLIST"}
    if set_env:
        d = tmp_path / "deny.sha256"
        d.write_text(deny_raw if deny_raw is not None else "".join(_digest(v) + "\n" for v in deny), encoding="utf-8")
        env["PUBLIC_DATA_DENYLIST"] = str(d)
    return subprocess.run([sys.executable, str(SCRIPT), str(f)], capture_output=True, text=True, env=env)


def test_clean_file_passes(tmp_path):
    assert run(tmp_path, "code = '699000001'\nvendor = 'TEST0001'\nname = '시험약국'\n").returncode == 0


def test_real_like_insurance_code_fails_without_echoing_value(tmp_path):
    forbidden = "645" + "301" + "230"
    r = run(tmp_path, f"code = {forbidden!r}\n")
    assert r.returncode == 1
    assert forbidden not in r.stdout + r.stderr
    assert "insurance_code" in r.stdout


def test_denylisted_supplier_code_fails(tmp_path):
    r = run(tmp_path, f"vendor = {SUPPLIER!r}\n")
    assert r.returncode == 1 and "denylisted" in r.stdout
    assert SUPPLIER not in r.stdout + r.stderr


def test_denylisted_pharmacy_name_fails_with_whitespace_variants(tmp_path):
    variant = PHARMACY.replace(" ", "")      # 공백 없는 표기도 같은 digest 로 걸린다
    for text in (f"name = {PHARMACY!r}\n", f"name = {variant!r}\n"):
        r = run(tmp_path, text)
        assert r.returncode == 1 and "denylisted" in r.stdout
        assert PHARMACY not in r.stdout + r.stderr and variant not in r.stdout + r.stderr


def test_allowlisted_synthetic_prefix_passes(tmp_path):
    assert run(tmp_path, "codes = ['699000012', '699000013']\n").returncode == 0


def test_missing_denylist_env_fails_closed(tmp_path):
    r = run(tmp_path, "x = 1\n", set_env=False)
    assert r.returncode == 2 and "config_error" in r.stdout


def test_empty_or_malformed_denylist_fails_closed(tmp_path):
    assert run(tmp_path, "x = 1\n", deny_raw="").returncode == 2
    assert run(tmp_path, "x = 1\n", deny_raw="not-a-digest\n").returncode == 2


def test_hex_hash_digits_are_not_insurance_code(tmp_path):
    sha = "6453" + "01230" + "abcdef0123456789abcdef0123456789"[:31]   # 40자 SHA 모양
    assert len(sha) == 40
    assert run(tmp_path, f"sha = {sha!r}\n").returncode == 0


def test_code_adjacent_to_hex_letters_is_still_detected(tmp_path):
    code = "645" + "301230"
    for text in (f"x = {'D' + code!r}\n", f"x = {code + 'abc'!r}\n"):
        r = run(tmp_path, text)
        assert r.returncode == 1 and "insurance_code" in r.stdout
        assert code not in r.stdout + r.stderr
