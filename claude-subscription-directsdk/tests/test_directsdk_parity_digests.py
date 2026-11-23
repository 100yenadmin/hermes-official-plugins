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


def test_a_native_reports_its_own_same_length_changes_a_few_times():
    g = gate()
    admission.observe_parity(g, body(rows('alpha', 'bravo', 'charlie')))
    admission.observe_parity(g, body(rows('alpha', 'brave', 'charlie', 'delta', 'echo')))
    admission.observe_parity(g, body(rows('alpha', 'bravx', 'charlie', 'delta', 'echo', 'fox', 'golf')))
    assert g.parity_notes == ['self_bytes rows=5 byte_diff=1 byte_role=a byte_block=0 byte_kinds=text/text',
                              'self_bytes rows=7 byte_diff=1 byte_role=a byte_block=0 byte_kinds=text/text']
    assert_metadata_only(g.parity_notes[0], 'brave', 'bravo')
    for n in range(5):  # a few per native, then quiet
        admission.observe_parity(g, body(rows('alpha', f'brav{n}', 'charlie', 'delta', 'echo', 'fox', 'golf', 'h', 'i')))
    assert len(g.parity_notes) == 3


def test_a_count_change_still_wins_the_self_line():
    g = gate()
    admission.observe_parity(g, body(rows('alpha', 'bravo', 'charlie')))
    admission.observe_parity(g, body(rows('alpha', 'bravo!', 'charlie', 'delta', 'echo')))
    assert g.parity_notes == ['self_diff row=1 role=a blocks=1/1 chars=5/6 rows=5 byte_diff=1 byte_role=a byte_block=0 byte_kinds=text/text']


def test_an_earlier_same_length_edit_is_named_beside_a_later_count_change():
    g = gate()
    admission.observe_parity(g, body(rows('alpha', 'bravo', 'charlie')))
    admission.observe_parity(g, body(rows('alphb', 'bravo', 'charlie!', 'delta', 'echo')))  # row 0 same length, row 2 longer
    assert g.parity_notes == ['self_diff row=2 role=u blocks=1/1 chars=7/8 rows=5 byte_diff=0 byte_role=u byte_block=0 byte_kinds=text/text']


def test_a_cache_control_key_inside_tool_input_is_data():
    def call(value):
        return [{'role': 'user', 'content': 'q'},
                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't', 'name': 'x', 'input': {'cache_control': value}}]}]
    assert admission._byte_diff(profile(call('aaaa')), profile(call('bbbb')))[0] == 1
    marked = call('aaaa')
    marked[1]['content'][0]['cache_control'] = {'type': 'ephemeral'}
    assert admission._byte_diff(profile(marked), profile(call('aaaa'))) is None


def test_odd_blocks_keep_byte_coverage():
    odd = [{'role': 'user', 'content': [{'type': {'not': 'hashable'}, 'text': 'x'}, {'type': 'text', 'text': 'a\ud800b'}]}]
    [row] = admission.row_digests(odd)
    assert [kind for kind, _ in row] == ['?', 'text']


def test_a_key_order_change_is_a_byte_change():
    a = [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't', 'name': 'x', 'input': {'a': 1, 'b': 2}}]}]
    b = [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't', 'name': 'x', 'input': {'b': 2, 'a': 1}}]}]
    assert admission._byte_diff(profile(b), profile(a))[0] == 0


def test_a_role_only_change_is_a_byte_change():
    a = [{'role': 'user', 'content': 'same'}, {'role': 'assistant', 'content': 'x'}]
    b = [{'role': 'assistant', 'content': 'same'}, {'role': 'assistant', 'content': 'x'}]
    assert admission._byte_diff(profile(b), profile(a)) == (0, 'byte_role=u/a byte_block=- byte_kinds=-/-')


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


def test_a_later_mixed_change_still_names_its_earlier_same_length_edit():
    g = gate()
    admission.observe_parity(g, body(rows('alpha', 'bravo', 'charlie')))
    admission.observe_parity(g, body(rows('alpha', 'bravo', 'charlie!', 'delta', 'echo')))  # count change: the self_diff
    admission.observe_parity(g, body(rows('alphb', 'bravo', 'charlie!!', 'delta', 'echo', 'fox', 'golf')))  # mixed
    assert g.parity_notes[1:] == ['self_bytes rows=7 byte_diff=0 byte_role=u byte_block=0 byte_kinds=text/text']
    admission.observe_parity(g, body(rows('alphb', 'bravo', 'charlie!!!', 'delta', 'echo', 'fox', 'golf', 'h', 'i')))
    assert len(g.parity_notes) == 2  # a count change alone after the latch stays quiet


def test_a_cold_prior_is_compared_once_and_names_the_rows_kinds_and_lengths():
    old = rows('alpha', 'bravo')
    new = rows('alpha', 'BRAVO!', 'charlie')
    g = gate()
    g.cold_prev = (9, profile(old))
    admission.observe_parity(g, body(new))
    [note] = g.parity_notes
    assert note == ('cold_prev=9 first_diff=1 role=a blocks=1/1 chars=5/6 rows=3/2 byte_diff=1 byte_role=a byte_block=0 '
                    'byte_kinds=text/text row_kinds=text1/text1 row_chars=5/6'), note
    assert g.cold_prev is None
    assert_metadata_only(note, 'BRAVO', 'bravo')
    admission.observe_parity(g, body(new + rows('delta')))
    assert not any(n.startswith('cold_prev=') for n in g.parity_notes[1:])


def test_a_cold_prior_names_an_attachment_row_by_its_kinds():
    old = [{'role': 'user', 'content': [{'type': 'text', 'text': 'note'},
                                        {'type': 'document', 'source': {'type': 'base64', 'data': 'QUJD'}}]}]
    new = [{'role': 'user', 'content': [{'type': 'text', 'text': 'note'}]}, *rows('x', 'y')[1:]]
    g = gate()
    g.cold_prev = (9, profile(old))
    admission.observe_parity(g, body(new))
    [note] = g.parity_notes
    assert ' row_kinds=text1.document1/text1 row_chars=8/4' in note, note
    assert_metadata_only(note, 'note', 'QUJD')


def test_an_unchanged_cold_prior_logs_none_and_no_row_fields():
    old = rows('alpha', 'bravo')
    g = gate()
    g.cold_prev = (9, profile(old))
    admission.observe_parity(g, body(old + rows('x', 'y')[1:]))
    [note] = g.parity_notes
    assert note.startswith('cold_prev=9 first_diff=none ') and note.endswith('byte_diff=none') and 'row_' not in note


def test_a_cold_prior_never_adds_a_cache_mark():
    old = rows('alpha', 'bravo')
    new = rows('alpha', 'bravo', *('x%d' % i for i in range(30)))
    new[-1]['content'][-1]['cache_control'] = {'type': 'ephemeral'}
    g = SimpleNamespace(first_turn={}, parity_prev=None, cold_prev=(9, profile(old)))
    payload = body(new)
    assert admission.mark_retired_prefix(g, payload) is payload


def test_a_sent_request_is_recorded_even_when_a_cancel_lands_after_the_send():
    seen = []
    g = SimpleNamespace(remember=seen.append, parity=('rows', 1), cancelled=True)
    admission.remember_sent(g)
    assert seen == [('rows', 1)]
    admission.remember_sent(SimpleNamespace(remember=None, parity=('rows', 1)))  # closed relay: nothing
    assert seen == [('rows', 1)]
