"""Find records in CRMs, databases and sheets: the exact provider requests, result shapes and safety."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.core.errors import NodeError
from app.integrations.providers import PROVIDERS, Conn
from tests.conftest import PG_URL, make_workflow, run, wf_def


def _conn(key: str, **values) -> Conn:
    return Conn(provider_key=key, config={k: v for k, v in values.items() if k not in ("api_key", "access_token", "password")},
                secret={k: v for k, v in values.items() if k in ("api_key", "access_token", "password")})


def _find(key: str, conn: Conn, **params) -> dict:
    return PROVIDERS[key].adapter.actions()["find_records"](conn, params)


def _query(req: httpx.Request) -> dict:
    return parse_qs(urlsplit(str(req.url)).query)


def test_hubspot_search_sends_server_side_filters(http_mock):
    http_mock.add("POST", "https://api.hubapi.com/crm/v3/objects/contacts/search",
                  (200, {"results": [{"id": "51", "properties": {"email": "ana@example.org", "firstname": "Ana"}}]}))
    out = _find("hubspot", _conn("hubspot", api_key="pat-1"), filters=[{"field": "email", "operator": "equals", "value": "ana@example.org"},
                                                                      {"field": "lifecyclestage", "operator": "is_not_empty"}],
                sort_by="createdate", order="descending", limit=5)
    body = json.loads(http_mock.requests[-1].content)
    assert body["filterGroups"][0]["filters"] == [{"propertyName": "email", "operator": "EQ", "value": "ana@example.org"},
                                                  {"propertyName": "lifecyclestage", "operator": "HAS_PROPERTY"}]
    assert body["sorts"] == [{"propertyName": "createdate", "direction": "DESCENDING"}] and body["limit"] == 5
    assert out["found"] and out["first"] == {"id": "51", "email": "ana@example.org", "firstname": "Ana"}


def test_airtable_builds_a_safe_formula(http_mock):
    http_mock.add("GET", "https://api.airtable.com/v0/appB/Leads", (200, {"records": [{"id": "rec1", "fields": {"Name": "Ana", "Score": 9}}]}))
    out = _find("airtable", _conn("airtable", api_key="pat", base_id="appB"), table="Leads", limit=10,
                filters=[{"field": "Name", "operator": "equals", "value": 'Ana") OR TRUE() OR ("'}, {"field": "Score", "operator": "gte", "value": "7"}])
    q = _query(http_mock.requests[-1])
    assert q["filterByFormula"] == ['AND(LOWER({Name}&\'\')=LOWER("Ana\\") OR TRUE() OR (\\""), {Score}>=7.0)']  # the quote stays inside the string
    assert q["maxRecords"] == ["10"] and out["records"] == [{"id": "rec1", "Name": "Ana", "Score": 9}]
    with pytest.raises(NodeError, match="needs a number"):
        _find("airtable", _conn("airtable", api_key="pat", base_id="appB"), table="Leads", filters=[{"field": "Score", "operator": "gt", "value": "high"}])


def test_supabase_uses_postgrest_filters(http_mock):
    http_mock.add("GET", "https://xyz.supabase.co/rest/v1/bookings", (200, [{"id": 3, "email": "a@b.co", "nights": 4}]))
    out = _find("supabase", _conn("supabase", project_url="https://xyz.supabase.co", api_key="key"), table="bookings", limit=20,
                filters=[{"field": "email", "operator": "contains", "value": "b.co"}, {"field": "nights", "operator": "gt", "value": 2}],
                sort_by="nights", order="descending")
    q = _query(http_mock.requests[-1])
    assert q["email"] == ["ilike.*b.co*"] and q["nights"] == ["gt.2"] and q["order"] == ["nights.desc.nullslast"] and q["limit"] == ["20"]
    assert out["count"] == 1


def test_sheets_notion_and_pipedrive_filter_what_their_apis_return(http_mock):
    http_mock.add("GET", "https://sheets.googleapis.com/v4/spreadsheets/S1/values/Leads!A:C",
                  (200, {"values": [["Name", "Email", "Score"], ["Ana", "ana@x.org", "9"], ["Bo", "bo@x.org", "3"], ["Cvet", "c@x.org"]]}))
    sheet = _find("google_sheets", _conn("google_sheets", access_token="t", spreadsheet_id="S1"), table="Leads!A:C",
                  filters=[{"field": "Score", "operator": "gte", "value": 5}])
    assert sheet["records"] == [{"Name": "Ana", "Email": "ana@x.org", "Score": "9", "_row": 2}]  # the row number lets a later step update it

    http_mock.add("POST", "https://api.notion.com/v1/databases/db1/query", (200, {"has_more": True, "results": [
        {"id": "p1", "url": "https://notion.so/p1", "properties": {"Name": {"type": "title", "title": [{"plain_text": "Launch post"}]},
                                                                   "Status": {"type": "status", "status": {"name": "Draft"}}}},
        {"id": "p2", "url": "https://notion.so/p2", "properties": {"Name": {"type": "title", "title": [{"plain_text": "Old post"}]},
                                                                   "Status": {"type": "status", "status": {"name": "Done"}}}}]}))
    notion = _find("notion", _conn("notion", api_key="secret"), table="db1", filters=[{"field": "Status", "operator": "equals", "value": "draft"}])
    assert [r["Name"] for r in notion["records"]] == ["Launch post"] and notion["note"]

    http_mock.add("GET", "https://acme.pipedrive.com/api/v1/persons/search", (200, {"data": {"items": [
        {"item": {"id": 7, "name": "Ana", "emails": ["ana@x.org"], "owner": {"id": 1}}}]}}))
    pd = _find("pipedrive", _conn("pipedrive", company_domain="acme", api_key="tok"), object_type="contacts",
               filters=[{"field": "email", "operator": "equals", "value": "ana@x.org"}])
    q = _query(http_mock.requests[-1])
    assert q["term"] == ["ana@x.org"] and q["fields"] == ["email"] and q["exact_match"] == ["true"]
    assert pd["first"]["id"] == 7 and pd["first"]["email"] == "ana@x.org"
    with pytest.raises(NodeError, match="at least one 'equals' or 'contains'"):
        _find("pipedrive", _conn("pipedrive", company_domain="acme", api_key="tok"), filters=[{"field": "value", "operator": "gt", "value": 5}])


def test_salesforce_escapes_soql_values(http_mock):
    http_mock.add("GET", "https://acme.my.salesforce.com/services/data/v60.0/query",
                  (200, {"records": [{"attributes": {"type": "Lead"}, "Id": "00Q1", "Email": "o'neil@x.org"}]}))
    out = _find("salesforce", _conn("salesforce", instance_url="https://acme.my.salesforce.com", access_token="t"), object_type="Lead",
                properties=["Email"], filters=[{"field": "Email", "operator": "equals", "value": "o'neil@x.org' OR Name != '"}], limit=3)
    q = _query(http_mock.requests[-1])["q"][0]
    assert q == "SELECT Id, Email FROM Lead WHERE Email = 'o\\'neil@x.org\\' OR Name != \\'' LIMIT 3"
    assert out["first"] == {"Id": "00Q1", "Email": "o'neil@x.org"}


def test_invalid_field_names_are_refused():
    with pytest.raises(NodeError, match="not valid"):
        _find("hubspot", _conn("hubspot", api_key="k"), filters=[{"field": "email\"; drop", "operator": "equals", "value": "x"}])


def test_a_workflow_finds_a_contact_and_branches_on_it(db, org, worker, http_mock):
    from app.integrations.service import create_integration

    http_mock.add("GET", "https://api.hubapi.com/account-info/v3/details", (200, {"portalId": 42}))
    http_mock.add("POST", "https://api.hubapi.com/crm/v3/objects/contacts/search",
                  lambda req: httpx.Response(200, json={"results": [{"id": "51", "properties": {"email": "ana@example.org"}}]}
                                             if "ana@" in req.content.decode() else {"results": []}))
    hub = create_integration(db, org.id, "hubspot", "CRM", {"api_key": "pat-test-123456"}, None)
    db.commit()
    wf = make_workflow(db, org, wf_def([
        {"id": "t", "type": "trigger.manual"},
        {"id": "find", "type": "action.crm_find", "config": {"integration_id": str(hub.id), "filters": [{"field": "email", "value": "{{ trigger.email }}"}]}},
        {"id": "known", "type": "logic.if", "config": {"condition": {"combinator": "and", "conditions": [{"left": "{{ nodes.find.found }}", "operator": "is_true"}]}}},
        {"id": "hello", "type": "logic.transform", "config": {"fields": {"contact": "{{ nodes.find.first.id }}"}}},
        {"id": "new", "type": "logic.transform", "config": {"fields": {"create": True}}},
    ], [("t", "find"), ("find", "known"), ("known", "hello", "true"), ("known", "new", "false")]))
    assert run(db, worker, wf, {"email": "ana@example.org"}).output == {"hello": {"contact": "51"}}
    assert run(db, worker, wf, {"email": "nobody@example.org"}).output == {"new": {"create": True}}


@pytest.mark.skipif(not PG_URL, reason="needs the PostgreSQL test database (WAE_TEST_DATABASE_URL)")
def test_postgresql_find_binds_every_value(monkeypatch):
    import psycopg

    from app.config import get_settings

    monkeypatch.setenv("WAE_ALLOW_PRIVATE_NETWORK", "true")
    get_settings.cache_clear()
    parts = urlsplit(PG_URL)
    conn = _conn("postgresql", host=parts.hostname, port=parts.port, database=parts.path.lstrip("/"), username=parts.username,
                 password=parts.password, sslmode="disable")
    with psycopg.connect(PG_URL.replace("postgresql://", "postgresql://", 1)) as pg:
        pg.execute("DROP TABLE IF EXISTS find_people")
        pg.execute("CREATE TABLE find_people (id serial primary key, name text, city text, spent numeric)")
        pg.execute("INSERT INTO find_people (name, city, spent) VALUES ('Ana','Varna',120), ('Bo','Sofia',40), ('O''Neil','Varna',NULL)")
    try:
        out = _find("postgresql", conn, table="find_people", filters=[{"field": "city", "operator": "equals", "value": "Varna"}],
                    sort_by="spent", order="descending")
        assert [r["name"] for r in out["records"]] == ["Ana", "O'Neil"]  # NULLs last
        evil = _find("postgresql", conn, table="find_people", filters=[{"field": "name", "operator": "equals", "value": "x' OR '1'='1"}])
        assert evil["count"] == 0
        like = _find("postgresql", conn, table="find_people", filters=[{"field": "name", "operator": "contains", "value": "%"}])
        assert like["count"] == 0  # wildcards in the search text are literal
    finally:
        with psycopg.connect(PG_URL) as pg:
            pg.execute("DROP TABLE IF EXISTS find_people")
