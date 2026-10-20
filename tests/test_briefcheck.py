"""Tests for the brief secret and sensitive-path filter."""

from __future__ import annotations

from crew_chief.briefcheck import check_brief

# --- cases from the brief --------------------------------------------------


def test_clean_brief_passes():
    text = "Implemente a função de média ponderada; não altere testes."
    assert check_brief(text, []) == []


def test_detects_openai_style_key_and_masks_it():
    findings = check_brief("use sk-abcdefghijklmnopqrstuv aqui", [])
    assert findings
    assert "sk-abcdefghijklmnopqrstuv" not in " ".join(findings)


def test_detects_env_block():
    text = "DATABASE_URL=postgres://localhost/db\nAPI_KEY=abc\nSECRET=x\n"
    findings = check_brief(text, [])
    assert findings
    assert any(f.startswith("env-block:") for f in findings)


def test_detects_sensitive_path_backslash():
    assert check_brief(r"leia referencia\dados.xlsx", ["referencia/"]) != []


def test_word_token_in_prose_is_not_flagged():
    assert check_brief("o token de sessão expira", []) == []


# --- masking and path-token rulings ---------------------------------------


def test_mask_keeps_only_first_four_characters():
    findings = check_brief("sk-abcdefghijklmnopqrstuv", [])
    assert findings == ["openai-key: sk-a…"]


def test_default_path_matches_at_a_token_boundary():
    assert check_brief("edite config/.env antes", []) != []
    assert check_brief("o environment muda", []) == []


def test_cookie_default_matches_cookie_prefixed_paths():
    assert check_brief("veja cookie_test.txt", []) != []


def test_sensitive_path_excerpt_is_the_entry_itself():
    findings = check_brief("abrir planilhas/vendas.xlsx", ["planilhas/"])
    assert findings == ["sensitive-path: planilhas"]


def test_identical_match_is_reported_once():
    assert check_brief("leia .env e depois .env de novo", []) == [
        "sensitive-path: .env"
    ]


def test_detects_cookie_header():
    findings = check_brief("Cookie: session=abc123456789", [])
    assert any(f.startswith("cookie-header:") for f in findings)


def test_path_is_sensitive_matches_at_token_boundaries():
    from crew_chief.briefcheck import path_is_sensitive

    entries = ["referencia/", "Planilhas"]
    assert path_is_sensitive("referencia/x.txt", entries)
    assert path_is_sensitive("docs/Referencia", entries)
    assert path_is_sensitive(r"data\planilhas\a.csv", entries)
    assert not path_is_sensitive("dereferencia/x.txt", entries)
    assert not path_is_sensitive("src/main.py", entries)
    assert not path_is_sensitive("referencia/x.txt", [])


def test_path_is_sensitive_accepts_fnmatch_entries():
    from crew_chief.briefcheck import path_is_sensitive

    assert path_is_sensitive("reports/2026.xlsx", ["*.xlsx"])
    assert path_is_sensitive("a.xlsx", ["*.xlsx"])
    assert not path_is_sensitive("reports/2026.csv", ["*.xlsx"])
