"""Round-trip checks for migration 0036 (citation metadata foundation).

pg_url is the SHARED session container; this test downgrades then upgrades,
so it MUST leave the schema at head for later tests — done in finally.
"""

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def _columns(conn, table):
    return {c["name"] for c in inspect(conn).get_columns(table)}


def test_0036_round_trip(migrated_engine, pg_url):
    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn:
            assert {
                "csl_json",
                "csl_json_extracted",
                "citekey",
                "metadata_status",
                "metadata_source",
                "original_sha256",
            } <= _columns(conn, "references")
            assert "citation_llm_usage" in inspect(conn).get_table_names()
            idx = {i["name"] for i in inspect(conn).get_indexes("citation_llm_usage")}
            assert "ix_citation_llm_usage_created" in idx
            assert "ix_citation_llm_usage_ws_created" in idx
            ref_checks = {c["name"] for c in inspect(conn).get_check_constraints("references")}
            assert "ck_references_metadata_status" in ref_checks
            assert "ck_references_original_sha256_hex" in ref_checks
            usage_checks = {
                c["name"] for c in inspect(conn).get_check_constraints("citation_llm_usage")
            }
            assert "ck_citation_llm_usage_status" in usage_checks
            assert "ck_citation_llm_usage_tokens_nonneg" in usage_checks

        command.downgrade(_cfg(pg_url), "0035")
        with engine.connect() as conn:
            added = {
                "csl_json",
                "csl_json_extracted",
                "citekey",
                "metadata_status",
                "metadata_source",
                "original_sha256",
            }
            assert added.isdisjoint(_columns(conn, "references"))
            assert "citation_llm_usage" not in inspect(conn).get_table_names()
            ref_checks = {c["name"] for c in inspect(conn).get_check_constraints("references")}
            assert "ck_references_metadata_status" not in ref_checks
            assert "ck_references_original_sha256_hex" not in ref_checks
    finally:
        # ALWAYS restore head, even if an assertion above failed — later tests
        # in this session reuse the shared container at head.
        command.upgrade(_cfg(pg_url), "head")
        engine.dispose()
