from datetime import date, datetime, timezone

import pytest

from iwiki_mcp.engine import frontmatter, trust


@pytest.mark.parametrize('verified', [
    'verified: {by: human:alice, at: 2026-11-26T03:00:00+03:00, evidence: {ids: [1, 2]}}',
    'verified:\n- by: human:alice\n  at: 2026-11-26T03:00:00+03:00\n  evidence:\n    ids: [1, 2]',
])
def test_imported_fields_survive_unrelated_edit(verified):
    source = ('---\ntitle: Original\ngenerated: {by: release-editor/2.3, at: 2026-11-26}\n'
              + verified + '\nsources:\n  - id: reference\n    details: {checked: true}\n'
              '---\n# Body\n')
    meta, body = frontmatter.split(source, strict_code=True)
    assert meta['generated'] == {'by': 'release-editor/2.3', 'at': '2026-11-26'}
    event = meta['verified'] if isinstance(meta['verified'], dict) else meta['verified'][0]
    assert event == {'by': 'human:alice', 'at': '2026-11-26T03:00:00+03:00',
                     'evidence': {'ids': [1, 2]}}
    assert meta['sources'] == [{'id': 'reference', 'details': {'checked': True}}]
    meta['title'] = 'Edited'
    rewritten = frontmatter.render(meta) + body
    assert frontmatter.split(rewritten, strict_code=True) == (meta, '# Body\n')


@pytest.mark.parametrize('invalid', [
    '{by: human:a, by: human:b}', '[unclosed', '!!python/object:bad {}',
])
def test_malformed_yaml_allows_read_but_protects_original_text(invalid):
    source = f'---\ntitle: X\nverified: {invalid}\n---\n## Heading\n'
    meta, body = frontmatter.split(source)
    assert body == '## Heading\n'
    assert meta['title'] == 'X'
    assert frontmatter.render(meta) + body == source
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.split(source, strict_code=True)


def test_trust_uses_current_events_only_in_either_order():
    meta = {'generated': {'at': '2026-11-26'}}
    old = {'by': 'human:alice', 'at': '2026-11-25T23:59:59.999999Z'}
    current = {'by': 'agent:release-editor/2.3', 'at': '2026-11-26T03:00:00+03:00'}
    for events in ([old, current], [current, old]):
        assert trust.tier({**meta, 'verified': events}) == 'machine-confirmed'
    assert trust.tier({'verified': old}) == 'human-reviewed'
    assert trust.tier({**meta, 'verified': old}) == 'unverified'
    assert trust.tier({'generated': {'at': 'broken'}, 'verified': current}) == 'unverified'
    assert trust.tier({'generated': None, 'verified': current}) == 'unverified'


@pytest.mark.parametrize(('actor', 'expected'), [
    ('release-editor/2.3', 'machine-confirmed'), ('agent:release-editor/2.3', 'machine-confirmed'),
    ('team:research:abc', 'machine-confirmed'), ('org+review:x', 'machine-confirmed'),
    ('human:alice', 'human-reviewed'), ('human:', 'unverified'), ('human:unknown', 'unverified'),
    ('agent:no-version', 'unverified'), ('no-scheme', 'unverified'), ('agent:/2', 'unverified'),
])
def test_recognizes_actor_conventions(actor, expected):
    assert trust.tier({'verified': {'by': actor, 'at': '2026-11-26'}}) == expected


@pytest.mark.parametrize('value', [date(2026, 11, 26), '2026-11-26', '2026-11-26t03:00:00+03:00',
                                   datetime(2026, 11, 26, tzinfo=timezone.utc)])
def test_date_representations_identify_same_utc_instant(value):
    assert trust.instant(value) == datetime(2026, 11, 26, tzinfo=timezone.utc)


@pytest.mark.parametrize('value', [True, '2026-11-26T00:00:00', '2026-11-26T01:00:00+02:75',
                                   '2026-11-31', '9999-12-31T23:59:59-01:00'])
def test_invalid_timestamp_is_ignored(value):
    assert trust.instant(value) is None


def test_roundtrip_preserves_yaml_types_in_imported_extensions():
    import yaml

    source = ('---\nx-evidence:\n  native: 2026-11-26\n'
              '  quoted: "2026-11-26"\n---\n# Body\n')
    meta, _ = frontmatter.split(source)
    block = frontmatter.render(meta).split('---\n')[1]
    assert yaml.safe_load(block)['x-evidence'] == {
        'native': date(2026, 11, 26), 'quoted': '2026-11-26',
    }


@pytest.mark.parametrize("field", ["!!binary YWJj", "!!set {source: null}", ".nan"])
def test_non_json_metadata_keeps_an_ordinary_page_readable(field):
    import json

    source = f"---\ntitle: Operations\nverified: {field}\n---\n## Checks\n"
    meta, body = frontmatter.split(source)
    assert json.loads(json.dumps(meta, allow_nan=False))["title"] == "Operations"
    assert frontmatter.render(meta) + body == source
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.split(source, strict_code=True)


@pytest.mark.parametrize(("checked", "expected"), [
    (".10000001", "unverified"), (".100000010", "unverified"),
    (".100000020", "human-reviewed"), (".10000003", "human-reviewed"),
])
def test_submicrosecond_ordering_of_imported_events(checked, expected):
    generated = "2026-11-26T03:00:00.10000002+03:00"
    event = {"by": "human:alice", "at": f"2026-11-26T00:00:00{checked}Z"}
    assert trust.tier({"generated": {"at": generated}, "verified": event}) == expected


def test_single_fractional_digit():
    assert trust.instant("2026-11-26T00:00:00.1Z") == datetime(
        2026, 11, 26, microsecond=100000, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("key", ["1", "null", "true"])
def test_non_string_nested_keys_cannot_collapse_in_public_metadata(key):
    import json

    block = f"verified: {{by: human:a, at: 2026-11-26, ext: {{{key}: first, '{key}': second}}}}"
    source = "---\ntitle: X\n" + block + "\n---\n## Details\n"
    meta, body = frontmatter.split(source)
    assert frontmatter.render(meta) + body == source
    assert json.loads(json.dumps(meta)) == meta
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.split(source, strict_code=True)
