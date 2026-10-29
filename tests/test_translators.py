"""Filter translation for each backend, checked without any database installed."""

from __future__ import annotations

import json
from unittest import mock

import pytest
from helpers import chroma_where_matches, filters, labelled_docs
from hypothesis import given, settings

from wardowl import And, In, Or, matches
from wardowl.backends import NothingMatches, chroma
from wardowl.backends.chroma import JSON_KEY, FilterTooLarge, _decode_metadata, _encode_metadata, filter_to_where
from wardowl.backends.pgvector import _EXACT_INTEGERS, filter_to_sql

# ---- PostgreSQL ------------------------------------------------------------------------------


def test_sql_uses_two_containment_tests_per_value_in_a_stable_order():
    sql, params = filter_to_sql(In("allowed_groups", {"hr", 7}))
    pair = "(metadata @> %s::jsonb OR metadata @> %s::jsonb)"
    assert sql == f"(({pair} AND {_EXACT_INTEGERS}) OR {pair})"
    assert [json.loads(p) for p in params[:2] + params[4:]] == [
        {"allowed_groups": 7},
        {"allowed_groups": [7]},
        {"allowed_groups": "hr"},
        {"allowed_groups": ["hr"]},
    ]
    assert params[2:4] == ["allowed_groups", ["7"]]  # integers must also read as integers, not as 7.0
    assert filter_to_sql(In("team", ["a"])) == (pair, ['{"team": "a"}', '{"team": ["a"]}'])


def test_sql_values_are_always_bound_never_inlined():
    sql, params = filter_to_sql(In("team'; DROP TABLE x; --", {"a' OR '1'='1"}))
    assert "DROP" not in sql and "'1'='1" not in sql
    assert json.loads(params[0]) == {"team'; DROP TABLE x; --": "a' OR '1'='1"}


def test_sql_edge_cases():
    assert filter_to_sql(None) == ("TRUE", [])
    assert filter_to_sql(In("team", [])) == ("FALSE", [])
    sql, params = filter_to_sql(And(In("a", {"x"}), Or(In("b", {"y"}), In("c", []))))
    assert sql.count(" AND ") == 1 and "FALSE" in sql and len(params) == 4


# ---- Chroma ----------------------------------------------------------------------------------


def test_where_splits_value_types_and_tests_one_key_per_list_value():
    assert filter_to_where(In("groups", {"hr", 7, "eng"})) == {
        "$or": [
            {"groups": {"$in": [7]}},
            {"groups": {"$in": ["eng", "hr"]}},
            {"groups::i:7": {"$eq": True}},
            {"groups::s:eng": {"$eq": True}},
            {"groups::s:hr": {"$eq": True}},
        ]
    }


def test_where_never_has_a_single_clause_and_or():
    where = filter_to_where(And(In("team", {"a"})))
    assert where == {"$or": [{"team": {"$in": ["a"]}}, {"team::s:a": {"$eq": True}}]}


def test_where_handles_filters_that_match_nothing():
    # Never {}: Chroma reads an empty where as no filter, which matches every record.
    for nothing in (In("team", []), And(In("team", {"a"}), In("level", [])), Or(In("team", []), In("level", []))):
        with pytest.raises(NothingMatches):
            filter_to_where(nothing)
    assert filter_to_where(Or(In("team", []), In("level", {1}))) == {
        "$or": [{"level": {"$in": [1]}}, {"level::i:1": {"$eq": True}}]
    }


@pytest.mark.parametrize("field", ["groups::s:hr", "a::b", "::", JSON_KEY])
def test_where_refuses_fields_that_address_list_keys_or_the_json_copy(field):
    # "groups::s:hr" would test the list key of {"groups": ["hr"]}; the reference finds no field of that name.
    with pytest.raises(ValueError, match="cannot be filtered"):
        filter_to_where(In(field, ["x"]))
    with pytest.raises(ValueError, match="cannot be filtered"):
        filter_to_where(Or(In("team", ["a"]), In(field, [])))


def _clauses(where: dict, op: str) -> list[dict]:
    """The clauses under a tree of `op` nodes, in order: the flat node the tree regroups."""
    ((key, body),) = where.items()
    return [clause for part in body for clause in _clauses(part, op)] if key == op else [where]


def test_where_nests_a_large_in_as_a_balanced_tree_of_small_nodes():
    # Chroma writes an $or as one binary OR per clause, so a flat one is as deep as it is long, and
    # SQLite refuses an expression more than 1,000 deep: every search of a subject in about 1,000
    # groups failed. The same clauses, 16 to a node, nest three levels deep here.
    groups = [f"group-{i:04d}" for i in range(2000)]
    where = filter_to_where(In("groups", groups))
    flat = [{"groups": {"$in": groups}}, *({f"groups::s:{g}": {"$eq": True}} for g in groups)]
    assert _clauses(where, "$or") == flat
    for g in groups[::97]:  # every node holds 2 to 16 clauses (checked on the way) and matches as before
        assert chroma_where_matches(where, {f"groups::s:{g}": True})
    assert not chroma_where_matches(where, {"groups::s:group-2000": True})
    cost = chroma._measure(where)
    assert cost.nesting == 3 and cost.depth <= 3 * chroma._FANOUT + chroma._LEAF_DEPTH
    assert cost.parameters == 1 + len(groups) + 2 * len(groups) + 135  # 126 + 8 + 1 nodes
    assert cost.characters == len(json.dumps(where))


@settings(max_examples=200, deadline=None)
@given(f=filters, docs=labelled_docs())
def test_where_trees_of_any_fanout_match_what_the_reference_matches(f, docs):
    # With 2 or 3 clauses per node, every filter here becomes a nested tree: it must match the
    # records the flat form matches, which are those the reference semantics match.
    flat = chroma._where(f)
    for fanout in (2, 3, chroma._FANOUT):
        with mock.patch.object(chroma, "_FANOUT", fanout):
            where = chroma._where(f)
        assert (where is None) is (flat is None)
        for doc in docs:
            stored = _encode_metadata(doc.id, doc.metadata)
            expected = matches(f, doc.metadata)
            assert (flat is not None and chroma_where_matches(flat, stored)) is expected
            assert (where is not None and chroma_where_matches(where, stored, fanout)) is expected


def _nested_ors(levels: int, width: int) -> Or | In:
    f: Or | In = In("a", ["x"])
    for level in range(levels):
        f = Or(f, *(In(f"b{level}_{j}", ["y"]) for j in range(width - 1)))
    return f


@pytest.mark.parametrize(
    ("f", "bound"),
    [
        (In("groups", [f"g{i}" for i in range(4000)]), "parameters"),
        (_nested_ors(16, 16), "depth"),
        (_nested_ors(40, 2), "nesting"),
        (In("groups", ["g" * 200_000, "h" * 200_000]), "characters"),
    ],
    ids=["parameters", "depth", "nesting", "characters"],
)
def test_where_beyond_any_one_bound_is_refused(f, bound):
    # Each bound alone refuses a where: nothing that large reaches Chroma (see wardowl/backends/chroma.py).
    cost = chroma._measure(chroma._where(f))
    limits = {
        "depth": chroma._MAX_DEPTH,
        "parameters": chroma._MAX_PARAMETERS,
        "nesting": chroma._MAX_NESTING,
        "characters": chroma._MAX_CHARACTERS,
    }
    assert {name for name, limit in limits.items() if getattr(cost, name) > limit} == {bound}
    with pytest.raises(FilterTooLarge, match="too large to send to Chroma"):
        filter_to_where(f)


def test_where_of_a_subject_in_40000_groups_is_refused():
    # With chromadb 1.5.9's embedded client, the flat $or of a subject in 40,000 groups ended the
    # Python process with SIGSEGV (the paper's filter-size experiment).
    with pytest.raises(FilterTooLarge) as refused:
        filter_to_where(In("allowed_groups", [f"group-{i:05d}" for i in range(40_000)]))
    assert isinstance(refused.value, ValueError)
    # 40,001 for the $in, two for each of 40,000 list keys, one for each of 2,669 nodes.
    assert "122,670 SQL parameters" in str(refused.value)


def test_a_where_too_large_to_send_keeps_the_part_every_match_meets():
    big = In("groups", [f"g{i}" for i in range(4000)])
    tenant, other = In("tenant", ["acme"]), In("tenant", ["globex"])

    def within(f):
        found = chroma._within(chroma._where(f))
        return None if found is None else found[0]

    # Every chunk an $and matches meets each of its clauses: those that fit are kept.
    assert within(And(tenant, big)) == filter_to_where(tenant)
    assert within(And(big, tenant, In("level", [1]))) == filter_to_where(And(tenant, In("level", [1])))
    # A chunk can match an $or through either clause, so no condition is left for both...
    assert within(Or(In("allowed_users", ["alice"]), big)) is None
    assert within(big) is None
    # ...unless each clause keeps one.
    assert within(Or(And(tenant, big), And(other, big))) == filter_to_where(Or(tenant, other))
    # A where that fits is kept as it is.
    where = chroma._where(And(tenant, In("groups", ["hr"])))
    assert chroma._within(where) == (where, chroma._measure(where))
    assert chroma._within(where)[0] is where


def test_chroma_metadata_never_uses_arrays_and_round_trips_exactly():
    metadata = {"groups": ["hr", 7, True, 1.5], "team": "eng", "flag": True, "score": 0.5, "none": None, "empty": []}
    stored = _encode_metadata("d", metadata)
    assert not any(isinstance(v, list) for v in stored.values())
    assert {k: v for k, v in stored.items() if k != JSON_KEY} == {
        "groups::s:hr": True,
        "groups::i:7": True,
        "team": "eng",
    }
    assert _decode_metadata(stored) == metadata


def test_chroma_records_without_a_trusted_copy_carry_no_permissions():
    assert _decode_metadata({"allowed_groups": "*"}) == {}
    assert _decode_metadata(None) == {}


@pytest.mark.parametrize("bad", [{"a::b": 1}, {JSON_KEY: "x"}, {"": 1}, {"blob": object()}])
def test_chroma_metadata_keys_and_values_are_validated(bad):
    with pytest.raises(ValueError):
        _encode_metadata("d", bad)
