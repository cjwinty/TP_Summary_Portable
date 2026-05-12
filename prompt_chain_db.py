"""
prompt_chain_db.py
==================
Database layer for the Prompt Chaining feature.

Schema
------
  prompt_chains          — one row per named chain
  prompt_chain_steps     — ordered steps belonging to a chain
  prompt_chain_runs      — execution history for a chain
  prompt_chain_run_steps — per-step results within a run

Design notes
------------
* Follows the same sqlite3 + threading.Lock pattern as database.py so it
  slots in without adding new dependencies.
* All public functions accept / return plain Python dicts, matching the
  project's existing conventions.
* Foreign-key enforcement is enabled per-connection (SQLite default is OFF).
* JSON is used for the `variables` bag on each step so arbitrary template
  values can be stored without schema changes.

Usage example
-------------
  from prompt_chain_db import (
      init_chain_db,
      save_chain, get_chain, list_chains, delete_chain,
      create_run, update_run_step, finish_run, get_run,
  )
  init_chain_db()

  # --- Save a new chain ------------------------------------------------
  chain_id = save_chain(
      name="Triage & Summarise",
      description="Classify the ticket then produce a customer-facing summary.",
      steps=[
          {
              "step_order": 1,
              "name": "Classify",
              "prompt_template": (
                  "You are a support triage agent.\n"
                  "Classify the following ticket into one of: Bug, Feature Request, "
                  "Question, or Complaint.\n\nTicket:\n{{input}}"
              ),
              "input_variable": "input",
              "output_variable": "classification",
          },
          {
              "step_order": 2,
              "name": "Summarise",
              "prompt_template": (
                  "You are a support summariser.\n"
                  "The ticket was classified as: {{classification}}\n\n"
                  "Write a concise one-paragraph summary for the customer.\n\n"
                  "Original ticket:\n{{input}}"
              ),
              "input_variable": "classification",   # primary chained input
              "output_variable": "summary",
          },
      ],
  )

  # --- Retrieve it -------------------------------------------------------
  chain = get_chain(chain_id)
  # chain["steps"] is a list of step dicts, sorted by step_order
"""

import sqlite3
import json
import threading
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Connection management (mirrors database.py pattern)
# ---------------------------------------------------------------------------

DB_PATH = "tp_cache.db"   # shared DB — same file as the rest of the app

_connection: sqlite3.Connection | None = None
_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    global _connection
    if _connection is None:
        _connection = sqlite3.connect(DB_PATH, check_same_thread=False)
        _connection.execute("PRAGMA journal_mode=WAL")
        _connection.execute("PRAGMA synchronous=NORMAL")
        _connection.execute("PRAGMA foreign_keys=ON")   # enforce FK constraints
        _connection.execute("PRAGMA cache_size=-64000")
        _connection.execute("PRAGMA temp_store=MEMORY")
    return _connection


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_chain_db() -> None:
    """Create all prompt-chaining tables if they do not already exist.

    Call this once at application start, alongside init_db().
    """
    conn = _get_conn()
    c = conn.cursor()

    # Migration: add step_type column if missing
    c.execute("PRAGMA table_info(prompt_chain_steps)")
    columns = {row[1] for row in c.fetchall()}
    if "step_type" not in columns:
        c.execute("ALTER TABLE prompt_chain_steps ADD COLUMN step_type TEXT NOT NULL DEFAULT 'llm'")

    # -- Master chain record ------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS prompt_chains (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            description TEXT    DEFAULT '',
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL
        )
    """)

    # -- Ordered steps within a chain ---------------------------------------
    # prompt_template  : Jinja-style template; {{input}} / {{classification}} etc.
    # input_variable   : name of the variable consumed by this step
    # output_variable  : key under which this step's output is stored
    # variables        : JSON blob for additional static template variables
    c.execute("""
        CREATE TABLE IF NOT EXISTS prompt_chain_steps (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            chain_id         INTEGER NOT NULL REFERENCES prompt_chains(id) ON DELETE CASCADE,
            step_order       INTEGER NOT NULL,
            name             TEXT    NOT NULL DEFAULT '',
            step_type        TEXT    NOT NULL DEFAULT 'llm',  -- 'llm' | 'db_query'
            prompt_template  TEXT    NOT NULL,
            input_variable   TEXT    NOT NULL DEFAULT 'input',
            output_variable  TEXT    NOT NULL DEFAULT 'output',
            variables        TEXT    NOT NULL DEFAULT '{}',
            created_at       TEXT    NOT NULL,
            UNIQUE (chain_id, step_order)
        )
    """)

    # -- Execution runs (one row per execute_chain() call) ------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS prompt_chain_runs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            chain_id      INTEGER NOT NULL REFERENCES prompt_chains(id) ON DELETE CASCADE,
            status        TEXT    NOT NULL DEFAULT 'running',  -- running | completed | failed
            initial_input TEXT    NOT NULL,
            final_output  TEXT    DEFAULT NULL,
            error         TEXT    DEFAULT NULL,
            started_at    TEXT    NOT NULL,
            finished_at   TEXT    DEFAULT NULL
        )
    """)

    # -- Per-step results within a run --------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS prompt_chain_run_steps (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id       INTEGER NOT NULL REFERENCES prompt_chain_runs(id) ON DELETE CASCADE,
            step_id      INTEGER NOT NULL REFERENCES prompt_chain_steps(id),
            step_order   INTEGER NOT NULL,
            input_sent   TEXT    NOT NULL,
            output_received TEXT  DEFAULT NULL,
            status       TEXT    NOT NULL DEFAULT 'pending',  -- pending | completed | failed
            error        TEXT    DEFAULT NULL,
            duration_ms  INTEGER DEFAULT NULL,
            executed_at  TEXT    DEFAULT NULL
        )
    """)

    # Indexes
    c.execute("CREATE INDEX IF NOT EXISTS idx_chain_steps_chain_id ON prompt_chain_steps(chain_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_chain_runs_chain_id  ON prompt_chain_runs(chain_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_run_steps_run_id     ON prompt_chain_run_steps(run_id)")

    conn.commit()


# ---------------------------------------------------------------------------
# Chain CRUD
# ---------------------------------------------------------------------------

def save_chain(name: str, steps: list[dict], description: str = "") -> int:
    """Insert a new chain with its steps and return the new chain ID.

    ``steps`` is a list of dicts with keys:
        step_order       (int)   – 1-based ordering
        name             (str)   – human label
        prompt_template  (str)   – template text; use {{variable_name}} placeholders
        input_variable   (str)   – which context key to pass as primary input
        output_variable  (str)   – key to store this step's output under
        variables        (dict)  – optional extra static variables (default {})
    """
    now = datetime.now().isoformat()
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute(
            "INSERT INTO prompt_chains (name, description, created_at, updated_at) VALUES (?,?,?,?)",
            (name, description, now, now),
        )
        chain_id = c.lastrowid

        for step in steps:
            c.execute("""
                INSERT INTO prompt_chain_steps
                    (chain_id, step_order, name, step_type, prompt_template,
                     input_variable, output_variable, variables, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                chain_id,
                step["step_order"],
                step.get("name", ""),
                step.get("step_type", "llm"),
                step["prompt_template"],
                step.get("input_variable", "input"),
                step.get("output_variable", "output"),
                json.dumps(step.get("variables", {})),
                now,
            ))

        conn.commit()
    return chain_id


def get_chain(chain_id: int) -> dict | None:
    """Return a chain dict with its steps, or None if not found.

    Return shape::

        {
            "id": 1,
            "name": "...",
            "description": "...",
            "created_at": "...",
            "updated_at": "...",
            "steps": [
                {
                    "id": 1, "chain_id": 1, "step_order": 1,
                    "name": "...", "prompt_template": "...",
                    "input_variable": "input", "output_variable": "classification",
                    "variables": {},
                },
                ...
            ]
        }
    """
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute(
            "SELECT id, name, description, created_at, updated_at FROM prompt_chains WHERE id=?",
            (chain_id,),
        )
        row = c.fetchone()
        if not row:
            return None

        chain = {
            "id": row[0], "name": row[1], "description": row[2],
            "created_at": row[3], "updated_at": row[4],
        }

        c.execute("""
            SELECT id, chain_id, step_order, name, step_type, prompt_template,
                   input_variable, output_variable, variables
            FROM prompt_chain_steps
            WHERE chain_id=? ORDER BY step_order
        """, (chain_id,))
        chain["steps"] = [
            {
                "id": r[0], "chain_id": r[1], "step_order": r[2], "name": r[3],
                "step_type": r[4], "prompt_template": r[5],
                "input_variable": r[6], "output_variable": r[7], "variables": json.loads(r[8]),
            }
            for r in c.fetchall()
        ]
    return chain


def list_chains() -> list[dict]:
    """Return summary rows for all chains (no steps)."""
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("""
            SELECT pc.id, pc.name, pc.description, pc.created_at, pc.updated_at,
                   COUNT(pcs.id) AS step_count
            FROM prompt_chains pc
            LEFT JOIN prompt_chain_steps pcs ON pcs.chain_id = pc.id
            GROUP BY pc.id ORDER BY pc.created_at DESC
        """)
        rows = c.fetchall()
    return [
        {
            "id": r[0], "name": r[1], "description": r[2],
            "created_at": r[3], "updated_at": r[4], "step_count": r[5],
        }
        for r in rows
    ]


def update_chain(chain_id: int, name: str | None = None,
                 description: str | None = None,
                 steps: list[dict] | None = None) -> bool:
    """Update chain metadata and/or replace its steps.

    Pass ``steps=None`` to leave existing steps untouched.
    Returns False if the chain does not exist.
    """
    now = datetime.now().isoformat()
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT id FROM prompt_chains WHERE id=?", (chain_id,))
        if not c.fetchone():
            return False

        if name is not None:
            c.execute("UPDATE prompt_chains SET name=?, updated_at=? WHERE id=?", (name, now, chain_id))
        if description is not None:
            c.execute("UPDATE prompt_chains SET description=?, updated_at=? WHERE id=?", (description, now, chain_id))

        if steps is not None:
            # Delete run steps first, then run records (for foreign key constraint)
            c.execute("""
                DELETE FROM prompt_chain_run_steps 
                WHERE step_id IN (SELECT id FROM prompt_chain_steps WHERE chain_id=?)
            """, (chain_id,))
            c.execute("DELETE FROM prompt_chain_runs WHERE chain_id=?", (chain_id,))
            
            # Now delete the old steps
            c.execute("DELETE FROM prompt_chain_steps WHERE chain_id=?", (chain_id,))
            for step in steps:
                c.execute("""
                    INSERT INTO prompt_chain_steps
                        (chain_id, step_order, name, step_type, prompt_template,
                         input_variable, output_variable, variables, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (
                    chain_id,
                    step["step_order"],
                    step.get("name", ""),
                    step.get("step_type", "llm"),
                    step["prompt_template"],
                    step.get("input_variable", "input"),
                    step.get("output_variable", "output"),
                    json.dumps(step.get("variables", {})),
                    now,
                ))
            c.execute("UPDATE prompt_chains SET updated_at=? WHERE id=?", (now, chain_id))

        conn.commit()
    return True


def delete_chain(chain_id: int) -> bool:
    """Delete a chain and all its steps / run history (CASCADE).

    Returns False if the chain did not exist.
    """
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT id FROM prompt_chains WHERE id=?", (chain_id,))
        if not c.fetchone():
            return False
        c.execute("DELETE FROM prompt_chains WHERE id=?", (chain_id,))
        conn.commit()
    return True


# ---------------------------------------------------------------------------
# Run persistence helpers  (used internally by prompt_chain_executor.py)
# ---------------------------------------------------------------------------

def create_run(chain_id: int, initial_input: str) -> int:
    """Insert a new run record and return its ID."""
    now = datetime.now().isoformat()
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("""
            INSERT INTO prompt_chain_runs (chain_id, status, initial_input, started_at)
            VALUES (?,?,?,?)
        """, (chain_id, "running", initial_input, now))
        run_id = c.lastrowid
        conn.commit()
    return run_id


def update_run_step(run_id: int, step_id: int, step_order: int,
                    input_sent: str, output_received: str | None = None,
                    status: str = "completed", error: str | None = None,
                    duration_ms: int | None = None) -> None:
    """Upsert a run-step result row."""
    now = datetime.now().isoformat()
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("""
            INSERT INTO prompt_chain_run_steps
                (run_id, step_id, step_order, input_sent, output_received,
                 status, error, duration_ms, executed_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT DO NOTHING
        """, (run_id, step_id, step_order, input_sent, output_received,
              status, error, duration_ms, now))
        conn.commit()


def finish_run(run_id: int, status: str, final_output: str | None = None,
               error: str | None = None) -> None:
    """Mark a run as completed or failed."""
    now = datetime.now().isoformat()
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("""
            UPDATE prompt_chain_runs
            SET status=?, final_output=?, error=?, finished_at=?
            WHERE id=?
        """, (status, final_output, error, now, run_id))
        conn.commit()


def get_run(run_id: int) -> dict | None:
    """Return a run dict with its step results, or None."""
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("""
            SELECT id, chain_id, status, initial_input, final_output,
                   error, started_at, finished_at
            FROM prompt_chain_runs WHERE id=?
        """, (run_id,))
        row = c.fetchone()
        if not row:
            return None
        run = {
            "id": row[0], "chain_id": row[1], "status": row[2],
            "initial_input": row[3], "final_output": row[4],
            "error": row[5], "started_at": row[6], "finished_at": row[7],
        }
        c.execute("""
            SELECT id, step_id, step_order, input_sent, output_received,
                   status, error, duration_ms, executed_at
            FROM prompt_chain_run_steps WHERE run_id=? ORDER BY step_order
        """, (run_id,))
        run["steps"] = [
            {
                "id": r[0], "step_id": r[1], "step_order": r[2],
                "input_sent": r[3], "output_received": r[4], "status": r[5],
                "error": r[6], "duration_ms": r[7], "executed_at": r[8],
            }
            for r in c.fetchall()
        ]
    return run


def list_runs(chain_id: int, limit: int = 50) -> list[dict]:
    """Return recent run summaries for a chain (no step detail)."""
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("""
            SELECT id, chain_id, status, initial_input, final_output,
                   error, started_at, finished_at
            FROM prompt_chain_runs
            WHERE chain_id=? ORDER BY started_at DESC LIMIT ?
        """, (chain_id, limit))
        rows = c.fetchall()
    return [
        {
            "id": r[0], "chain_id": r[1], "status": r[2],
            "initial_input": r[3][:120], "final_output": r[4],
            "error": r[5], "started_at": r[6], "finished_at": r[7],
        }
        for r in rows
    ]
