"""
Regression coverage for v1.1.6: ``webui.app._save_env`` must preserve the
operator's current ``.env`` values for keys absent from the POST payload,
instead of resetting them to the ``.env.example`` template defaults.

Why this matters.  Since v1.1.0 the front-end ``saveSettings`` flow only
POSTs *dirty* keys (those whose input value differs from the page-render
baseline) to ``/api/env``.  The original ``_save_env`` implementation
iterated ``.env.example`` and treated "key not in payload" as "emit the
raw template line" — silently replacing real API keys (and every other
unchanged-but-template-present value) with the ``.env.example`` default
on every save.  Operators reported real-world data loss: editing one
unrelated setting wiped their OpenRouter / Alibaba API keys.

These tests pin four scenarios end-to-end against the actual
``_save_env`` helper (no mocks of the helper itself), so any future
regression of the merge-then-write contract is caught immediately.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture
def webui_module(tmp_path, monkeypatch):
    """Import ``webui.app`` with ``ENV_FILE`` / ``ENV_EXAMPLE`` redirected
    to per-test tmp files.  Cancels the module-level eviction timer to
    avoid leaking ``threading.Timer`` daemons across the test session.
    """
    from webui import app as webui_app
    importlib.reload(webui_app)
    env_file = tmp_path / ".env"
    env_example = tmp_path / ".env.example"
    monkeypatch.setattr(webui_app, "ENV_FILE", env_file)
    monkeypatch.setattr(webui_app, "ENV_EXAMPLE", env_example)
    try:
        yield webui_app
    finally:
        _t = getattr(webui_app, "_eviction_timer", None)
        if _t is not None:
            try:
                _t.cancel()
            except Exception:
                pass


def _write(p: Path, body: str) -> None:
    p.write_text(body, encoding="utf-8")


# ─── 1. Unchanged-key preservation (the v1.1.6 bug) ──────────────────────────

def test_unchanged_keys_keep_current_env_value_not_template_default(webui_module):
    """The reproducer: operator has a real OpenRouter key in ``.env``;
    they save an unrelated setting (only that key in the POST payload).
    Result must keep the real key, not regress to the placeholder.
    """
    _write(webui_module.ENV_EXAMPLE, (
        "OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxx\n"
        "LLM_PROVIDER=openrouter\n"
        "STRICT_JSON=1\n"
    ))
    _write(webui_module.ENV_FILE, (
        "OPENROUTER_API_KEY=sk-or-v1-REAL-KEY-12345\n"
        "LLM_PROVIDER=openrouter\n"
        "STRICT_JSON=1\n"
    ))

    # Front-end POSTs only the dirty key — STRICT_JSON flipped off.
    webui_module._save_env({"STRICT_JSON": "0"})

    out = webui_module.ENV_FILE.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=sk-or-v1-REAL-KEY-12345" in out, \
        "real OpenRouter key must survive a partial save"
    assert "sk-or-v1-xxxxxxxxxxxxxxxxxxxx" not in out, \
        ".env.example placeholder must NOT leak into .env"
    assert "STRICT_JSON=0" in out
    assert "LLM_PROVIDER=openrouter" in out


def test_dirty_keys_overwrite_current_value(webui_module):
    _write(webui_module.ENV_EXAMPLE, (
        "OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxx\n"
        "STRICT_JSON=1\n"
    ))
    _write(webui_module.ENV_FILE, (
        "OPENROUTER_API_KEY=sk-or-v1-OLD\n"
        "STRICT_JSON=1\n"
    ))

    webui_module._save_env({"OPENROUTER_API_KEY": "sk-or-v1-NEW"})

    out = webui_module.ENV_FILE.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=sk-or-v1-NEW" in out
    assert "sk-or-v1-OLD" not in out
    assert "sk-or-v1-xxxxxxxxxxxxxxxxxxxx" not in out


def test_orphan_keys_in_env_only_survive(webui_module):
    """Keys present in ``.env`` but NOT in ``.env.example`` (operator-only
    overrides) must be appended after the template body — not silently
    dropped.  Before v1.1.6 these keys were also at risk depending on
    POST payload contents.
    """
    _write(webui_module.ENV_EXAMPLE, "LLM_PROVIDER=openrouter\n")
    _write(webui_module.ENV_FILE, (
        "LLM_PROVIDER=openrouter\n"
        "CRUCIBLE_INTERNAL_OVERRIDE=custom-value\n"
    ))

    # Empty payload — operator clicked Save with no dirty keys.
    webui_module._save_env({})

    out = webui_module.ENV_FILE.read_text(encoding="utf-8")
    assert "CRUCIBLE_INTERNAL_OVERRIDE=custom-value" in out
    assert "LLM_PROVIDER=openrouter" in out


def test_empty_post_payload_is_idempotent(webui_module):
    _write(webui_module.ENV_EXAMPLE, (
        "OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxx\n"
        "STRICT_JSON=1\n"
    ))
    _write(webui_module.ENV_FILE, (
        "OPENROUTER_API_KEY=sk-or-v1-REAL\n"
        "STRICT_JSON=0\n"
    ))
    snapshot_before = webui_module.ENV_FILE.read_text(encoding="utf-8")

    webui_module._save_env({})

    out = webui_module.ENV_FILE.read_text(encoding="utf-8")
    # The function rewrites the file, so byte-equality isn't expected,
    # but every real value must round-trip.
    assert "OPENROUTER_API_KEY=sk-or-v1-REAL" in out
    assert "STRICT_JSON=0" in out
    # Defensive: make sure the placeholder did NOT sneak in even on the
    # "save nothing" path (the path most likely to regress).
    assert "sk-or-v1-xxxxxxxxxxxxxxxxxxxx" not in out
    # And that the snapshot would have shown the same real value.
    assert "sk-or-v1-REAL" in snapshot_before


def test_no_env_yet_falls_back_to_post_payload(webui_module):
    """First-run case: ``.env`` doesn't exist yet, only ``.env.example``."""
    _write(webui_module.ENV_EXAMPLE, (
        "OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxx\n"
        "STRICT_JSON=1\n"
    ))
    assert not webui_module.ENV_FILE.exists()

    webui_module._save_env({"OPENROUTER_API_KEY": "sk-or-v1-FRESH"})

    out = webui_module.ENV_FILE.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=sk-or-v1-FRESH" in out
    # The other template-only key has no current value AND no POST entry
    # — the original raw line is preserved verbatim (the placeholder
    # *only* survives here because nothing has ever overridden it).
    assert "STRICT_JSON=1" in out


def test_template_missing_writes_merged_payload(webui_module):
    """Defensive: when ``.env.example`` is somehow gone, ``_save_env``
    still merges current ``.env`` with the POST payload — it does NOT
    drop the operator's existing values just because the template was
    deleted.
    """
    _write(webui_module.ENV_FILE, (
        "OPENROUTER_API_KEY=sk-or-v1-REAL\n"
        "STRICT_JSON=0\n"
    ))
    assert not webui_module.ENV_EXAMPLE.exists()

    webui_module._save_env({"STRICT_JSON": "1"})

    out = webui_module.ENV_FILE.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=sk-or-v1-REAL" in out
    assert "STRICT_JSON=1" in out


# ─── 2. Comment / template-structure preservation ──────────────────────────

def test_template_comments_and_blank_lines_preserved(webui_module):
    """``.env.example`` comments and section headers must survive a save
    so the on-disk ``.env`` remains human-readable.
    """
    _write(webui_module.ENV_EXAMPLE, (
        "# Provider selection\n"
        "LLM_PROVIDER=openrouter\n"
        "\n"
        "# OpenRouter\n"
        "OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxx\n"
    ))
    _write(webui_module.ENV_FILE, (
        "# Provider selection\n"
        "LLM_PROVIDER=openrouter\n"
        "\n"
        "# OpenRouter\n"
        "OPENROUTER_API_KEY=sk-or-v1-REAL\n"
    ))

    webui_module._save_env({"LLM_PROVIDER": "ollama"})

    out = webui_module.ENV_FILE.read_text(encoding="utf-8")
    assert "# Provider selection" in out
    assert "# OpenRouter" in out
    assert "LLM_PROVIDER=ollama" in out
    assert "OPENROUTER_API_KEY=sk-or-v1-REAL" in out
    assert "sk-or-v1-xxxxxxxxxxxxxxxxxxxx" not in out


# ─── 3. Structural pin — the merge step still lives in _save_env ───────────

def test_save_env_source_still_merges_current_env_first(webui_module):
    """Belt-and-braces: any future refactor that drops the
    ``current = _load_env() if ENV_FILE.exists() else {}`` + ``merged``
    construction will silently reintroduce the v1.1.6 data-loss bug.
    This structural assertion makes that regression loud.
    """
    import inspect
    src = inspect.getsource(webui_module._save_env)
    assert "_load_env()" in src, \
        "_save_env must call _load_env() to read the current .env state"
    assert "merged" in src, \
        "_save_env must build a 'merged' dict over the current .env values"
