"""Script tests for create_key.py and add_credits.py.

Runs the CLIs as subprocesses against a temporary SQLite database so we test
the real entry points (arg parsing, output, exit codes), not imports. Each
test gets an isolated DATABASE_URL and ADMIN_TOKEN via env; no secrets.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PYTHON = sys.executable


@pytest.fixture()
def env(tmp_path):
    e = os.environ.copy()
    e["ADMIN_TOKEN"] = "test-admin-token"
    e["DATABASE_URL"] = f"sqlite:///{tmp_path / 'scripts.db'}"
    e["SOLARI_API_KEY"] = ""
    return e


def run_script(script, *args, env):
    return subprocess.run(
        [PYTHON, str(SCRIPTS / script), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def db_rows(env, query):
    path = env["DATABASE_URL"].replace("sqlite:///", "")
    con = sqlite3.connect(path)
    try:
        return con.execute(query).fetchall()
    finally:
        con.close()


# --- create_key.py ---------------------------------------------------------------


def test_create_key_prints_raw_key_once(env):
    result = run_script("create_key.py", "--email", "a@example.com", "--credits", "10",
                        env=env)
    assert result.returncode == 0, result.stderr
    assert "ek_live_" in result.stdout
    assert "will not be shown again" in result.stdout
    key = [tok for tok in result.stdout.split() if tok.startswith("ek_live_")][0]
    rows = db_rows(env, "SELECT key_last4, credits, email FROM api_keys")
    assert rows == [(key[-4:], 10, "a@example.com")]
    # hash stored, plaintext absent
    hashes = db_rows(env, "SELECT key_hash FROM api_keys")
    assert all(len(h[0]) == 64 and key not in h[0] for h in hashes)


def test_create_key_default_credits_from_config(env):
    env["FREE_CREDITS"] = "40"
    result = run_script("create_key.py", env=env)
    assert result.returncode == 0
    assert db_rows(env, "SELECT credits FROM api_keys") == [(40,)]


def test_create_key_writes_signup_ledger(env):
    run_script("create_key.py", "--credits", "5", env=env)
    rows = db_rows(env, "SELECT amount, balance_after, reason FROM credit_ledger")
    assert rows == [(5, 5, "initial_free_credits")]


# --- add_credits.py ----------------------------------------------------------------


@pytest.fixture()
def created_key(env):
    result = run_script("create_key.py", "--credits", "3", env=env)
    assert result.returncode == 0
    return [tok for tok in result.stdout.split() if tok.startswith("ek_live_")][0]


def test_add_credits_by_key_id(env, created_key):
    result = run_script("add_credits.py", "--key-id", "1", "--amount", "50",
                        "--reason", "topup", env=env)
    assert result.returncode == 0, result.stderr
    assert "3 -> 53" in result.stdout
    rows = db_rows(env, "SELECT amount, balance_after, reason FROM credit_ledger ORDER BY id")
    assert rows == [(3, 3, "initial_free_credits"), (50, 53, "topup")]


def test_add_credits_by_raw_key(env, created_key):
    result = run_script("add_credits.py", "--key", created_key, "--amount", "7", env=env)
    assert result.returncode == 0, result.stderr
    assert db_rows(env, "SELECT credits FROM api_keys") == [(10,)]


def test_add_credits_rejects_unknown_key(env, created_key):
    result = run_script("add_credits.py", "--key-id", "99", "--amount", "5", env=env)
    assert result.returncode == 1
    assert "no api key" in result.stderr


def test_add_credits_rejects_inactive_key(env, created_key):
    con = sqlite3.connect(env["DATABASE_URL"].replace("sqlite:///", ""))
    con.execute("UPDATE api_keys SET is_active = 0")
    con.commit()
    con.close()
    result = run_script("add_credits.py", "--key", created_key, "--amount", "5", env=env)
    assert result.returncode == 1
    assert "inactive" in result.stderr or "not found" in result.stderr


def test_add_credits_rejects_non_positive_amount(env, created_key):
    result = run_script("add_credits.py", "--key-id", "1", "--amount", "0", env=env)
    assert result.returncode != 0
    result = run_script("add_credits.py", "--key-id", "1", "--amount", "-3", env=env)
    assert result.returncode != 0


def test_add_credits_requires_exactly_one_target(env, created_key):
    result = run_script("add_credits.py", "--amount", "5", env=env)
    assert result.returncode != 0
    result = run_script("add_credits.py", "--key", created_key, "--key-id", "1",
                        "--amount", "5", env=env)
    assert result.returncode != 0
