"""SpiceDBAuthorizer against a scripted HTTP server, and against a real SpiceDB when one is available."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from helpers import corpora, oracle_top_k, queries, subjects
from hypothesis import given, settings
from hypothesis import strategies as st

from wardowl import (
    AuthorizationError,
    CachingAuthorizer,
    Document,
    Hit,
    In,
    InMemoryBackend,
    PermissionedRetriever,
    Subject,
    UnsupportedSubject,
)
from wardowl.authorizers.spicedb import SpiceDBAuthorizer
from wardowl.testing import check_authorizer

ANN = Subject("ann", ["eng"])
HITS = [Hit("c1", "t", 1.0, {"doc_id": "d1"}), Hit("c2", "t", 1.0, {"doc_id": "d2"}), Hit("c3", "t", 1.0, {})]


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, payload, dict(self.headers)))
        status, body = self.server.responder(self.path, payload)
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.requests = []
    httpd.responder = lambda path, payload: (500, {})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield httpd
    httpd.shutdown()


def _bulk(allowed):
    """A CheckBulkPermissions responder granting the (document, subject) pairs in `allowed`."""

    def respond(path, payload):
        pairs = []
        for item in payload["items"]:
            subject = item["subject"]
            who = subject["object"]["objectId"]
            if "optionalRelation" in subject:
                who += "#" + subject["optionalRelation"]
            ok = (item["resource"]["objectId"], who) in allowed
            permissionship = "PERMISSIONSHIP_HAS_PERMISSION" if ok else "PERMISSIONSHIP_NO_PERMISSION"
            pairs.append({"request": item, "item": {"permissionship": permissionship}})
        return 200, {"pairs": list(reversed(pairs))}  # out of order: answers are matched by request

    return respond


def _lookup(*lines):
    return lambda path, payload: (200, b"\n".join(json.dumps(line).encode() for line in lines) + b"\n")


# ---- scripted server -------------------------------------------------------------------------


def test_by_default_only_the_user_is_asked_about(server):
    # SpiceDB follows the memberships it stores, and applies exclusions and intersections to them.
    server.responder = _bulk({("d1", "eng#member"), ("d2", "ann")})
    assert SpiceDBAuthorizer(server.url).check(ANN, HITS) == [False, True, False]
    asked = {i["subject"]["object"]["objectType"] for _, p, _ in server.requests for i in p["items"]}
    assert asked == {"user"}


def test_check_asks_for_the_user_and_each_group_and_matches_answers_by_request(server):
    server.responder = _bulk({("d1", "eng#member")})
    authz = SpiceDBAuthorizer(server.url, "secret", consistency={"fullyConsistent": True}, group_subjects=True)
    assert authz.check(ANN, HITS) == [True, False, False]  # c3 has no doc_id: denied without asking
    path, payload, headers = server.requests[0]
    assert path == "/v1/permissions/checkbulk" and headers["Authorization"] == "Bearer secret"
    assert payload["consistency"] == {"fullyConsistent": True}
    user = {"object": {"objectType": "user", "objectId": "ann"}}
    group = {"object": {"objectType": "group", "objectId": "eng"}, "optionalRelation": "member"}
    asked = {json.dumps(i["subject"], sort_keys=True) for i in payload["items"]}
    assert asked == {json.dumps(user, sort_keys=True), json.dumps(group, sort_keys=True)}
    assert {i["resource"]["objectId"] for i in payload["items"]} == {"d1", "d2"}


def test_group_subjects_can_be_turned_off_and_batches_are_split(server):
    server.responder = _bulk({("d2", "ann")})
    authz = SpiceDBAuthorizer(server.url, group_subjects=False, batch_size=1)
    assert authz.check(ANN, HITS) == [False, True, False]
    assert len(server.requests) == 2 and all(len(p["items"]) == 1 for _, p, _ in server.requests)


@pytest.mark.parametrize(
    "answer",
    [
        {"pairs": [{"request": {}, "error": {"code": 3, "message": "bad id"}}]},
        {"pairs": "nope"},
        {"pairs": []},  # a question with no answer
        {"pairs": [{"item": {"permissionship": "PERMISSIONSHIP_HAS_PERMISSION"}}]},  # no request echoed
    ],
)
def test_malformed_and_failed_bulk_answers_fail_closed(server, answer):
    server.responder = lambda path, payload: (200, answer)
    with pytest.raises(AuthorizationError):
        SpiceDBAuthorizer(server.url, group_subjects=False).check(ANN, HITS[:1])


def test_a_conditional_answer_is_not_a_decision(server):
    def conditional(path, payload):
        item = payload["items"][0]
        return 200, {"pairs": [{"request": item, "item": {"permissionship": "PERMISSIONSHIP_CONDITIONAL_PERMISSION"}}]}

    server.responder = conditional
    with pytest.raises(AuthorizationError, match="caveat"):
        SpiceDBAuthorizer(server.url, group_subjects=False).check(ANN, HITS[:1])


def test_caveat_context_is_sent_with_every_question(server):
    server.responder = _bulk(set())
    authz = SpiceDBAuthorizer(server.url, group_subjects=False, caveat_context=lambda s: {"region": "eu"})
    authz.check(ANN, HITS[:1])
    assert server.requests[0][1]["items"][0]["context"] == {"region": "eu"}
    broken = SpiceDBAuthorizer(server.url, caveat_context=lambda s: 1 / 0)
    with pytest.raises(AuthorizationError, match="caveat_context") as failure:
        broken.check(ANN, HITS[:1])
    assert "division" not in str(failure.value) and isinstance(failure.value.__cause__, ZeroDivisionError)


def test_filter_for_reads_the_lookup_stream(server):
    has = "LOOKUP_PERMISSIONSHIP_HAS_PERMISSION"
    server.responder = _lookup({"result": {"resourceObjectId": "d1", "permissionship": has}})
    assert SpiceDBAuthorizer(server.url).filter_for(ANN) == In("doc_id", ["d1"])
    assert [p["subject"]["object"]["objectId"] for _, p, _ in server.requests] == ["ann"]
    # With group_subjects=True, one lookup for the user and one for each group, merged.
    server.requests.clear()
    assert SpiceDBAuthorizer(server.url, group_subjects=True).filter_for(ANN) == In("doc_id", ["d1"])
    assert [p["subject"]["object"]["objectId"] for _, p, _ in server.requests] == ["ann", "eng"]
    assert server.requests[0][1]["optionalLimit"] == 1000


def test_total_timeout_bounds_every_lookup_and_batch_of_one_call_together(server):
    has = "LOOKUP_PERMISSIONSHIP_HAS_PERMISSION"
    lookup = _lookup({"result": {"resourceObjectId": "d1", "permissionship": has}})
    bulk = _bulk(set())

    def slow(path, payload):
        time.sleep(0.15)
        return (lookup if path.endswith("/resources") else bulk)(path, payload)

    server.responder = slow
    many = Subject("ann", [f"g{i}" for i in range(20)])  # one lookup each, one after another
    authz = SpiceDBAuthorizer(server.url, group_subjects=True, batch_size=1, timeout=2.0, total_timeout=0.4)
    with pytest.raises(AuthorizationError, match="total_timeout ran out") as failure:
        authz.filter_for(many)
    assert isinstance(failure.value.__cause__, TimeoutError) and len(server.requests) <= 3
    server.requests.clear()
    with pytest.raises(AuthorizationError, match="total_timeout ran out"):
        authz.check(many, HITS)  # 2 resources x 21 subjects, one pair per request
    assert len(server.requests) <= 3
    server.requests.clear()
    # Each call has its own deadline: a quick call after a slow one is not refused.
    server.responder = lambda path, payload: (lookup if path.endswith("/resources") else bulk)(path, payload)
    assert authz.filter_for(ANN) == In("doc_id", ["d1"])


def test_a_possibly_truncated_or_conditional_lookup_gives_no_filter(server):
    has = "LOOKUP_PERMISSIONSHIP_HAS_PERMISSION"
    server.responder = _lookup(*({"result": {"resourceObjectId": f"d{i}", "permissionship": has}} for i in range(3)))
    assert SpiceDBAuthorizer(server.url, lookup_limit=3).filter_for(ANN) is None
    server.responder = _lookup(
        {"result": {"resourceObjectId": "d1", "permissionship": "LOOKUP_PERMISSIONSHIP_CONDITIONAL_PERMISSION"}}
    )
    assert SpiceDBAuthorizer(server.url).filter_for(ANN) is None


def test_an_empty_lookup_gives_no_filter_so_the_check_decides(server):
    # SpiceDB marks no end to the stream: a stream cut off before its first result, by a proxy or
    # a fault, reads exactly as a subject who may read nothing.
    def respond(path, payload):
        if path == "/v1/permissions/resources":
            return 200, b""
        return _bulk({("d2", "ann")})(path, payload)

    server.responder = respond
    assert SpiceDBAuthorizer(server.url).filter_for(ANN) is None
    assert SpiceDBAuthorizer(server.url, trust_empty_lookup=True).filter_for(ANN) == In("doc_id", [])
    backend = InMemoryBackend([Document("c1", "leave", {"doc_id": "d1"}), Document("c2", "leave", {"doc_id": "d2"})])
    result = PermissionedRetriever(backend, SpiceDBAuthorizer(server.url)).retrieve("leave", ANN)
    assert [h.id for h in result.hits] == ["c2"] and result.strategy == "postfilter"
    # strategy="prefilter" has no filter to apply; trusting the empty lookup gives one that matches nothing.
    with pytest.raises(ValueError, match="prefilter"):
        PermissionedRetriever(backend, SpiceDBAuthorizer(server.url), strategy="prefilter").retrieve("leave", ANN)
    trusting = SpiceDBAuthorizer(server.url, trust_empty_lookup=True)
    assert PermissionedRetriever(backend, trusting, strategy="prefilter").retrieve("leave", ANN).hits == []


def test_max_staleness_follows_the_consistency_unless_it_is_given():
    # Read by CachingAuthorizer, which keeps nothing for that long after invalidate().
    assert SpiceDBAuthorizer("http://127.0.0.1:1").max_staleness == 11.0  # minimizeLatency, twice SpiceDB's 5.5 s
    assert SpiceDBAuthorizer("http://127.0.0.1:1", consistency={"fullyConsistent": True}).max_staleness == 0.0
    token = {"atLeastAsFresh": {"token": "GhUKEzE2NjE"}}
    assert SpiceDBAuthorizer("http://127.0.0.1:1", consistency=token).max_staleness == 11.0
    assert SpiceDBAuthorizer("http://127.0.0.1:1", consistency=token, max_staleness=0).max_staleness == 0.0
    assert CachingAuthorizer(SpiceDBAuthorizer("http://127.0.0.1:1", max_staleness=3)).source_staleness == 3.0
    for bad in (-1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError, match="staleness"):
            SpiceDBAuthorizer("http://127.0.0.1:1", max_staleness=bad)


@pytest.mark.parametrize(
    "line",
    [{"error": {"code": 5, "message": "not found"}}, {"result": {"permissionship": "x"}}, ["not", "an", "object"]],
)
def test_lookup_errors_fail_closed(server, line):
    server.responder = _lookup(line)
    with pytest.raises(AuthorizationError):
        SpiceDBAuthorizer(server.url).filter_for(ANN)


def test_a_resource_field_that_is_not_one_string_never_passes_check(server):
    # The In pre-filter would match ["d1", "d2"] by either element; check denies it without asking,
    # so the default re-check drops such a chunk.
    server.responder = _bulk({("d1", "ann"), ("d2", "ann")})
    odd = [Hit("c1", "t", 1.0, {"doc_id": ["d1", "d2"]}), Hit("c2", "t", 1.0, {"doc_id": 7})]
    assert SpiceDBAuthorizer(server.url, group_subjects=False).check(ANN, odd) == [False, False]
    assert server.requests == []


@pytest.mark.parametrize("resource_field", ["doc_id", None])
def test_a_resource_id_spicedb_would_refuse_denies_only_its_chunk(server, resource_field):
    # One such id used to go into the CheckBulkPermissions request, which SpiceDB refuses whole, so
    # every retrieval that found the chunk returned nothing, for every user.
    bad = ["", "a b", "a:b", "a#b", "a.b", "a@b", "*", "\xe9", "a\n", "x" * 1025]
    good = ["a", "A-z_0/9|=+", "x" * 1024]
    server.responder = _bulk({(i, "ann") for i in bad + good})
    hits = [Hit(i, "t", 1.0, {"doc_id": i}) for i in bad + good]
    authz = SpiceDBAuthorizer(server.url, resource_field=resource_field, group_subjects=False)
    assert authz.check(ANN, hits) == [False] * len(bad) + [True] * len(good)
    sent = [i["resource"]["objectId"] for _, payload, _ in server.requests for i in payload["items"]]
    assert sorted(sent) == sorted(good)


@pytest.mark.parametrize(
    ("subject", "group_subjects"),
    [
        (Subject("zo\xeb"), False),  # users of the conformance kit's own data
        (Subject("zoe\u0308"), False),
        (Subject("7.0"), False),
        (Subject("ann@example.com"), False),
        (Subject("*"), False),  # the subjects' wildcard
        (Subject("u" * 1025), False),
        (Subject("ann", ["\xe9quipe"]), True),
        (Subject("ann", ["eng", "a b"]), True),
    ],
)
def test_a_subject_spicedb_cannot_name_is_refused_before_anything_is_sent(server, subject, group_subjects):
    # SpiceDB refused every CheckBulkPermissions request naming such an id with HTTP 400, which the
    # conformance campaign recorded as errors. It is now refused up front, with a ValueError as well.
    server.responder = _bulk({("d1", "ann")})
    authz = SpiceDBAuthorizer(server.url, group_subjects=group_subjects)
    with pytest.raises(UnsupportedSubject, match="cannot be sent to SpiceDB") as raised:
        authz.check(subject, HITS)
    assert isinstance(raised.value, AuthorizationError) and isinstance(raised.value, ValueError)
    with pytest.raises(UnsupportedSubject, match="cannot be sent to SpiceDB"):
        authz.filter_for(subject)
    assert server.requests == []


def test_groups_are_checked_only_when_they_are_sent(server):
    server.responder = _bulk({("d2", "ann")})
    assert SpiceDBAuthorizer(server.url).check(Subject("ann", ["\xe9quipe"]), HITS) == [False, True, False]
    assert SpiceDBAuthorizer(server.url).check(Subject("u" * 1024), HITS[:1]) == [False]


def test_without_a_resource_field_chunks_are_resources_and_there_is_no_filter(server):
    server.responder = _bulk({("c2", "ann")})
    authz = SpiceDBAuthorizer(server.url, resource_field=None, group_subjects=False)
    assert authz.check(ANN, HITS) == [False, True, False]
    assert authz.filter_for(ANN) is None


def test_configuration_errors():
    with pytest.raises(ValueError):
        SpiceDBAuthorizer("ftp://spicedb")
    with pytest.raises(ValueError):
        SpiceDBAuthorizer("http://127.0.0.1:1", batch_size=0)
    with pytest.raises(AuthorizationError, match="action"):
        SpiceDBAuthorizer("http://127.0.0.1:1").check(ANN, HITS, action="delete")
    assert SpiceDBAuthorizer("http://127.0.0.1:1").check(ANN, []) == []


def test_an_unreachable_spicedb_fails_closed():
    retriever = PermissionedRetriever(
        InMemoryBackend([Document("c1", "leave", {"doc_id": "d1"})]),
        SpiceDBAuthorizer("http://127.0.0.1:1", timeout=0.5),
    )
    with pytest.raises(AuthorizationError):
        retriever.retrieve("leave", ANN)


# ---- a real SpiceDB --------------------------------------------------------------------------

SPICEDB_URL = os.environ.get("WARDOWL_TEST_SPICEDB_URL")
SPICEDB_TOKEN = os.environ.get("WARDOWL_TEST_SPICEDB_TOKEN", "")
needs_spicedb = pytest.mark.skipif(not SPICEDB_URL, reason="set WARDOWL_TEST_SPICEDB_URL to run against SpiceDB")

SCHEMA = """
definition user {}

definition group {
    relation member: user
}

definition document {
    relation viewer: user | user:* | group#member
    permission view = viewer
}
"""


def _api(path, payload):
    request = urllib.request.Request(  # noqa: S310 - test helper, URL from the test environment
        f"{SPICEDB_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {SPICEDB_TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.loads(response.read() or b"{}")


def _relationships_for(docs):
    """Replace every document relationship with those the metadata ACLs describe."""
    _api("/v1/schema/write", {"schema": SCHEMA})
    _api("/v1/relationships/delete", {"relationshipFilter": {"resourceType": "document"}})
    updates = []
    for d in docs:
        resource = {"objectType": "document", "objectId": d.id}
        subjects_ = [{"object": {"objectType": "user", "objectId": u}} for u in d.metadata.get("allowed_users", [])]
        for g in d.metadata.get("allowed_groups", []):
            if g == "*":
                subjects_.append({"object": {"objectType": "user", "objectId": "*"}})
            else:
                subjects_.append({"object": {"objectType": "group", "objectId": g}, "optionalRelation": "member"})
        updates += [
            {"operation": "OPERATION_TOUCH", "relationship": {"resource": resource, "relation": "viewer", "subject": s}}
            for s in subjects_
        ]
    for i in range(0, len(updates), 100):
        _api("/v1/relationships/write", {"updates": updates[i : i + 100]})


def _authorizer(**options):
    # The reference oracle reads groups from the subject, and these schemas exclude nothing, so the
    # groups are checked as subject sets: memberships are not stored in SpiceDB here.
    options = {"group_subjects": True, **options}
    return SpiceDBAuthorizer(SPICEDB_URL, SPICEDB_TOKEN, consistency={"fullyConsistent": True}, **options)


@needs_spicedb
@settings(max_examples=40, deadline=None)
@given(
    docs=corpora(max_size=12),
    subject=subjects,
    query=queries,
    k=st.integers(min_value=1, max_value=6),
    strategy=st.sampled_from(["auto", "prefilter", "postfilter"]),
)
def test_spicedb_gives_exactly_the_reference_answer(docs, subject, query, k, strategy):
    _relationships_for(docs)
    stored = [Document(d.id, d.text, {**d.metadata, "doc_id": d.id}) for d in docs]
    # Pre-filtering needs a filter for every subject, also one whose lookup finds nothing.
    authorizer = _authorizer(trust_empty_lookup=strategy == "prefilter")
    retriever = PermissionedRetriever(
        InMemoryBackend(stored), authorizer, strategy=strategy, max_candidates=len(docs) + k
    )
    assert [h.id for h in retriever.retrieve(query, subject, k).hits] == oracle_top_k(docs, subject, query, k)


@needs_spicedb
def test_spicedb_passes_the_conformance_kit():
    docs = [
        Document("doc-a", "t", {"allowed_users": ["alice"]}),
        Document("doc-b", "t", {"allowed_groups": ["hr"]}),
        Document("doc-c", "t", {"allowed_groups": ["*"]}),
        Document("doc-d", "t", {}),
    ]
    _relationships_for(docs)
    hits = [Hit(d.id, d.text, 1.0, {**d.metadata, "doc_id": d.id}) for d in docs]
    people = [Subject("alice"), Subject("bob", ["hr"]), Subject("carol", ["eng", "hr"]), Subject("dave")]
    check_authorizer(_authorizer(), people, hits)


EXCLUSION = SCHEMA.replace(
    "    permission view = viewer\n", "    relation banned: user\n    permission view = viewer - banned\n"
)


def _touch(resource_type, resource_id, relation, subject):
    resource = {"objectType": resource_type, "objectId": resource_id}
    return {
        "operation": "OPERATION_TOUCH",
        "relationship": {"resource": resource, "relation": relation, "subject": subject},
    }


def _user(name):
    return {"object": {"objectType": "user", "objectId": name}}


@needs_spicedb
def test_a_banned_member_of_a_permitted_group_is_denied_by_default():
    # view = viewer - banned, and the document is shared with a group mallory belongs to. With the
    # memberships stored in SpiceDB, it applies the exclusion to mallory, whatever the group may do.
    assert "viewer - banned" in EXCLUSION
    eng = {"object": {"objectType": "group", "objectId": "wo-eng"}, "optionalRelation": "member"}
    _api("/v1/schema/write", {"schema": EXCLUSION})
    try:
        _api("/v1/relationships/delete", {"relationshipFilter": {"resourceType": "document"}})
        updates = [
            _touch("group", "wo-eng", "member", _user("ann")),
            _touch("group", "wo-eng", "member", _user("mallory")),
            _touch("document", "wo-plan", "viewer", eng),
            _touch("document", "wo-plan", "banned", _user("mallory")),
        ]
        _api("/v1/relationships/write", {"updates": updates})
        hits = [Hit("c1", "t", 1.0, {"doc_id": "wo-plan"})]
        default = SpiceDBAuthorizer(SPICEDB_URL, SPICEDB_TOKEN, consistency={"fullyConsistent": True})
        mallory, ann = Subject("mallory", ["wo-eng"]), Subject("ann", ["wo-eng"])
        assert default.check(ann, hits) == [True] and default.check(mallory, hits) == [False]
        backend = InMemoryBackend([Document("c1", "launch plan", {"doc_id": "wo-plan"})])
        # LookupResources applies the exclusion too: it finds the plan for ann, and nothing for
        # mallory, which gives no filter, so "auto" post-filters her and the check denies her.
        assert default.filter_for(ann) == In("doc_id", ["wo-plan"]) and default.filter_for(mallory) is None
        for strategy in ("auto", "postfilter"):
            retriever = PermissionedRetriever(backend, default, strategy=strategy)
            assert retriever.retrieve("plan", mallory).hits == []
            assert [h.id for h in retriever.retrieve("plan", ann).hits] == ["c1"]
        # The hazard the documentation warns of: the group, asked about on its own, is not banned.
        assert _authorizer().check(mallory, hits) == [True]
    finally:  # the other tests write SCHEMA, which SpiceDB refuses while banned relationships exist
        _api("/v1/relationships/delete", {"relationshipFilter": {"resourceType": "document"}})
        _api("/v1/relationships/delete", {"relationshipFilter": {"resourceType": "group"}})
        _api("/v1/schema/write", {"schema": SCHEMA})
