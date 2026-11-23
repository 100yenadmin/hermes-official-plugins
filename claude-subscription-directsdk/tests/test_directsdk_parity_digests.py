"""Session parity lines carry the first row whose bytes changed, from keyed per-block digests: a same-length edit
that the counts cannot see is found, marks are not row bytes, and no digest or text reaches the log."""
import json
import re
from types import SimpleNamespace

import admission
import directsdk


def rows(*texts):
    out = []
    for i, text in enumerate(texts):
        out.append({'role': 'user' if i % 2 == 0 else 'assistant', 'content': [{'type': 'text', 'text': text}]})
    return out


def body(messages):
    return json.dumps({'messages': messages}).encode()


def gate(handed=None):
    return SimpleNamespace(first_turn={}, parity_prev=handed, parity=None, cancelled=False, parity_logged=False,
                           parity_notes=[])


def profile(messages):
    return admission.row_profile(messages) + (admission.row_digests(messages),)


SAFE = re.compile(r"^[a-z_]+=[A-Za-z0-9_\-/:.]+$")


def assert_metadata_only(note, *texts):
    for field in note.split():
        assert field in ('self_bytes', 'self_diff') or SAFE.match(field), field
    for text in texts:
        assert text not in note


def test_a_same_length_edit_is_found_by_bytes_but_not_by_counts():
    old = rows('alpha', 'bravo', 'charlie')
    new = rows('alpha', 'brave', 'charlie', 'delta')  # row 1 keeps its length
    g = gate((7, profile(old)))
    admission.observe_parity(g, body(new))
    [note] = g.parity_notes
    assert 'first_diff=none' in note
    assert 'byte_diff=1 byte_role=a byte_block=0 byte_kinds=text/text' in note
    assert_metadata_only(note, 'brave', 'bravo')


def test_moved_marks_are_not_a_byte_change():
    old = rows('alpha', 'bravo', 'charlie')
    old[2]['content'][0]['cache_control'] = {'type': 'ephemeral'}
    new = rows('alpha', 'bravo', 'charlie', 'delta')
    new[3]['content'][0]['cache_control'] = {'type': 'ephemeral', 'ttl': '1h'}
    g = gate((7, profile(old)))
    admission.observe_parity(g, body(new))
    assert g.parity_notes[0].endswith('byte_diff=none')


def test_a_hand_over_without_digests_reads_a_dash():
    old = rows('alpha', 'bravo')
    g = gate((7, admission.row_profile(old)))  # an older profile: counts only
    admission.observe_parity(g, body(rows('alpha', 'bravo', 'charlie')))
    assert g.parity_notes[0].endswith('byte_diff=-')


def test_a_missing_or_retyped_block_names_both_kinds():
    old = rows('alpha', 'bravo')
    new = [old[0], {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't', 'name': 'x', 'input': {}}]},
           {'role': 'user', 'content': 'next'}]
    byte = admission._byte_diff(profile(new), profile(old))
    assert byte == (1, 'byte_role=a byte_block=0 byte_kinds=text/tool_use')
    shorter = [old[0]]
    assert admission._byte_diff(profile(shorter), profile(old)) == (1, 'byte_role=a/- byte_block=0 byte_kinds=text/-')


def test_a_native_reports_its_own_same_length_change_once():
    g = gate()
    admission.observe_parity(g, body(rows('alpha', 'bravo', 'charlie')))
    admission.observe_parity(g, body(rows('alpha', 'brave', 'charlie', 'delta', 'echo')))
    admission.observe_parity(g, body(rows('alpha', 'bravx', 'charlie', 'delta', 'echo', 'fox', 'golf')))
    assert g.parity_notes == ['self_bytes rows=5 byte_diff=1 byte_role=a byte_block=0 byte_kinds=text/text']
    assert_metadata_only(g.parity_notes[0], 'brave', 'bravo')


def test_a_count_change_still_wins_the_self_line():
    g = gate()
    admission.observe_parity(g, body(rows('alpha', 'bravo', 'charlie')))
    admission.observe_parity(g, body(rows('alpha', 'bravo!', 'charlie', 'delta', 'echo')))
    assert g.parity_notes == ['self_diff row=1 role=a blocks=1/1 chars=5/6 rows=5']


def test_three_part_profiles_keep_the_count_diff_and_the_retired_prefix_mark_working():
    old, new = profile(rows('a', 'b', 'c')), profile(rows('a', 'b', 'd', 'e'))
    assert admission._parity_diff(new, old) is None  # same counts
    assert admission._parity_diff(profile(rows('a', 'bb', 'c')), old)[0] == 1


def test_the_digest_key_is_per_process_and_never_unkeyed():
    import hashlib
    block = {'type': 'text', 'text': 'yes'}
    [(kind, digest)] = admission.row_digests([{'role': 'user', 'content': [block]}])[0:1][0]
    plain = hashlib.sha256(json.dumps(block, sort_keys=True, separators=(',', ':')).encode()).digest()[:8]
    assert kind == 'text' and digest != plain


def test_unknown_block_kinds_are_not_logged_by_name():
    messages = [{'role': 'user', 'content': [{'type': 'secret-looking-kind', 'x': 1}]}]
    assert admission.row_digests(messages)[0][0][0] == '?'


def test_a_suffix_divergence_counts_results_a_native_cannot_take():
    key = directsdk._message_key
    consumed = [key({'role': 'user', 'content': 'u'})]
    tail = [{'role': 'tool', 'tool_call_id': 'a', 'content': 'text'},
            {'role': 'tool', 'tool_call_id': 'b', 'content': [{'type': 'image_url', 'image_url': {'url': 'x'}}]}]
    line = directsdk._divergence(consumed, consumed + [None, None], [{}] + tail, {'a', 'b'}, 'suffix')
    assert line.endswith('nonstr=1') and 'matched_ids=2' in line
