"""Session.21: a rebuild's extra cache mark goes at the last row equal to the retired native's last request
(setting ``rebuild_boundary_mark``, default off), and row digests no longer flag a live tool result as changed only
because native writes its keys in another order. Shapes are eva's 2026-11-24 rebuilds (row numbers from the logs)."""
import inspect
import json
from types import SimpleNamespace

import pytest

import admission

MARK = {'type': 'ephemeral', 'ttl': '1h'}
SYSTEM = [{'type': 'text', 'text': 'sys', 'cache_control': dict(MARK)}]
TOOLS = [{'name': 'mcp__hermes__lookup', 'input_schema': {'type': 'object'}}]


def tool_result(j, live):
    content = [{'type': 'text', 'text': f'result {j} ' * 4}]
    if live:  # native's own key order for an MCP result it received
        return {'tool_use_id': f't{j - 1}', 'type': 'tool_result', 'content': content}
    return {'type': 'tool_result', 'tool_use_id': f't{j - 1}', 'content': content}  # the replay's order


def logical(n, spawn=None):
    """Row 0 a user text, then assistant tool calls and user tool results in turn; rows from ``spawn`` on are live."""
    rows = [{'role': 'user', 'content': [{'type': 'text', 'text': 'opening ' * 6}]}]
    for j in range(1, n):
        if j % 2:
            rows.append({'role': 'assistant', 'content': [{'type': 'text', 'text': f'step {j}'},
                                                          {'type': 'tool_use', 'id': f't{j}', 'name': 'mcp__hermes__lookup', 'input': {'k': j}}]})
        else:
            rows.append({'role': 'user', 'content': [tool_result(j, spawn is not None and j >= spawn)]})
    return rows


SYS_ROW = {'role': 'system', 'content': [{'type': 'text', 'text': 's' * 139}]}


def request(messages):
    messages = json.loads(json.dumps(messages))
    messages[-1]['content'][-1]['cache_control'] = dict(MARK)
    return {'model': 'claude-x', 'system': json.loads(json.dumps(SYSTEM)), 'tools': TOOLS, 'messages': messages}


def handed(body):
    m = body['messages']
    return (77, admission.row_profile(m) + (admission.row_digests(m), admission.request_identity(body)))


def shape(spawn, s_at, n_old, n_new):
    """The retired native's last request (live rows from ``spawn``, its native-only system row at ``s_at``) and the
    rebuild's first request (all replayed, no system row, ``n_new`` rows)."""
    old_rows = logical(n_old - 1, spawn)
    old = request(old_rows[:s_at] + [SYS_ROW] + old_rows[s_at:])
    new_rows = logical(n_new)
    if len(new_rows) % 2 == 0 and new_rows[-1]['role'] == 'assistant':
        pass
    return old, request(new_rows)


def gate_for(old, on=True):
    return SimpleNamespace(first_turn={}, parity_prev=handed(old), boundary_mark=on, boundary_note='')


SHAPES = {'05:48Z': (192, 300, 356, 357), '08:40Z': (302, 306, 330, 330), '08:47Z': (330, 334, 374, 375)}


@pytest.mark.parametrize('name', sorted(SHAPES))
def test_a_rebuild_marks_the_last_row_before_the_native_only_system_row(name):
    spawn, s_at, n_old, n_new = SHAPES[name]
    old, new = shape(spawn, s_at, n_old, n_new)
    gate = gate_for(old)
    out = json.loads(admission.mark_last_equal_row(gate, json.dumps(new).encode()))
    b = s_at - 1
    assert out['messages'][b]['content'][-1]['cache_control'] == MARK  # the last equal row, not the spawn row
    assert all('cache_control' not in blk for r in out['messages'][:b] for blk in r['content'])
    assert out['messages'][-1]['content'][-1]['cache_control'] == MARK  # native's own end mark stays
    assert gate.boundary_note == f' boundary={b}.{len(out["messages"][b]["content"]) - 1}'
    assert gate.parity_prev is not None  # observe_parity still consumes the hand-over
    # Session.20's mark (counts only, every row unchanged) adds nothing here: the case this setting exists for.
    assert admission.mark_retired_prefix(SimpleNamespace(first_turn={}, parity_prev=handed(old)), json.dumps(new).encode()) \
        == json.dumps(new).encode()


def test_a_live_tool_result_in_natives_key_order_is_the_same_row_bytes():
    # Eva 11-24 15:03 (+07): byte_diff=379 was the retired native's first live tool result (spawn 378), yet the rebuild
    # read past it. Same keys, other order: the API reads the same prefix, so the digests must agree.
    live, replay = tool_result(4, True), tool_result(4, False)
    assert list(live) != list(replay)
    assert admission.row_digests([{'role': 'user', 'content': [live]}]) == admission.row_digests([{'role': 'user', 'content': [replay]}])
    # The 15:03 shape on the parity line: byte_diff now names the native-only system row (382), not row 379.
    old, new = shape(378, 382, 386, 385)
    g = SimpleNamespace(first_turn={}, parity_prev=handed(old), parity=None, cancelled=False, parity_logged=False,
                        parity_notes=[], cold_prev=None)
    admission.observe_parity(g, json.dumps(new).encode())
    assert ' byte_diff=382 ' in g.parity_notes[0] + ' ', g.parity_notes
    # A real same-length edit is still seen.
    edited = json.loads(json.dumps(new))
    edited['messages'][10]['content'][0]['content'][0]['text'] = edited['messages'][10]['content'][0]['content'][0]['text'].replace('result', 'RESULT')
    g2 = SimpleNamespace(first_turn={}, parity_prev=handed(old), parity=None, cancelled=False, parity_logged=False,
                         parity_notes=[], cold_prev=None)
    admission.observe_parity(g2, json.dumps(edited).encode())
    assert ' byte_diff=10 ' in g2.parity_notes[0] + ' '
    # Key order inside data (tool input) still counts: only protocol fields are sorted.
    a = [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't', 'name': 'x', 'input': {'a': 1, 'b': 2}}]}]
    b = [{'role': 'assistant', 'content': [{'id': 't', 'type': 'tool_use', 'name': 'x', 'input': {'b': 2, 'a': 1}}]}]
    assert admission.row_digests(a) != admission.row_digests(b)


def test_the_parity_line_carries_the_boundary_field():
    old, new = shape(302, 306, 330, 330)
    gate = gate_for(old)
    gate.parity, gate.cancelled, gate.parity_logged, gate.parity_notes, gate.cold_prev = None, False, False, [], None
    payload = admission.mark_last_equal_row(gate, json.dumps(new).encode())
    admission.observe_parity(gate, payload)
    assert gate.parity_notes[0].endswith(' boundary=305.1'), gate.parity_notes


def test_no_change_left_means_the_session17_place_checked_byte_for_byte():
    rows = logical(41)
    old = request(rows[:20])
    new = request(rows)
    out = json.loads(admission.mark_last_equal_row(gate_for(old), json.dumps(new).encode()))
    assert out['messages'][19]['content'][-1]['cache_control'] == MARK
    # Nothing after the retired rows: no mark (native's own end mark is already there).
    same = json.dumps(old).encode()
    assert admission.mark_last_equal_row(gate_for(old), same) is same


def test_refusals_forward_the_payload_unchanged():
    old, new = shape(302, 306, 330, 330)
    payload = json.dumps(new).encode()
    other_tools = json.loads(payload)
    other_tools['tools'] = [{'name': 'mcp__hermes__other', 'input_schema': {'type': 'object'}}]
    other_effort = json.loads(payload)
    other_effort['output_config'] = {'effort': 'medium'}
    first_row = json.loads(payload)
    first_row['messages'][0]['content'][0]['text'] += '!'
    full = json.loads(payload)
    for i in (50, 60):
        full['messages'][i]['content'][-1]['cache_control'] = dict(MARK)
    no_digests = SimpleNamespace(first_turn={}, parity_prev=(77, admission.row_profile(old['messages'])), boundary_note='')
    cases = [
        (gate_for(old), json.dumps(other_tools).encode(), ' boundary=-:identity'),
        (gate_for(old), json.dumps(other_effort).encode(), ' boundary=-:identity'),
        (gate_for(old), json.dumps(first_row).encode(), ' boundary=-:row0'),
        (gate_for(old), json.dumps(full).encode(), ' boundary=-:marks'),
        (no_digests, payload, ' boundary=-:no_digests'),
    ]
    for gate, data, note in cases:
        assert admission.mark_last_equal_row(gate, data) is data
        assert gate.boundary_note == note
    for gate in (SimpleNamespace(first_turn={}, parity_prev=None), SimpleNamespace(first_turn=None, parity_prev=handed(old))):
        assert admission.mark_last_equal_row(gate, payload) is payload  # not a rebuild; per call
    assert admission.mark_last_equal_row(gate_for(old), b'not json') == b'not json'


def test_a_thinking_ended_row_walks_back_and_a_system_row_is_never_marked():
    old_rows = logical(40)
    old_rows[19]['content'].append({'type': 'thinking', 'thinking': 'x', 'signature': 'sig'})
    new_rows = json.loads(json.dumps(old_rows))
    new_rows[20]['content'][0]['content'][0]['text'] += 'changed'
    old, new = request(old_rows[:30]), request(new_rows)
    out = json.loads(admission.mark_last_equal_row(gate_for(old), json.dumps(new).encode()))
    assert 'cache_control' not in out['messages'][19]['content'][-1]  # the thinking block
    assert out['messages'][19]['content'][-2]['cache_control'] == MARK  # the tool_use before it
    # The row before the first difference is a system row: refused.
    s_rows = old_rows[:20] + [SYS_ROW] + old_rows[20:]
    s_new = json.loads(json.dumps(s_rows))
    s_new[21]['content'][-1]['text'] = 'other'
    payload = json.dumps(request(s_new)).encode()
    gate = gate_for(request(s_rows[:30]))
    assert admission.mark_last_equal_row(gate, payload) is payload
    assert gate.boundary_note == ' boundary=-:row20'


def test_off_by_default_and_the_relay_picks_the_function_by_setting():
    import directsdk_setup
    assert directsdk_setup.REBUILD_BOUNDARY_MARK_SETTING == 'rebuild_boundary_mark'
    source = inspect.getsource(admission.Handler.do_POST)
    assert "(mark_last_equal_row if getattr(gate, 'boundary_mark', False) else mark_retired_prefix)(gate, payload)" in source
    assert source.index('mark_retired_prefix)(gate, payload)') < source.index('observe_parity(gate, payload)')
    init = inspect.getsource(admission.Admission.__init__) if hasattr(admission, 'Admission') else ''
    assert 'self.boundary_mark, self.boundary_note = False, ' in inspect.getsource(admission)


def test_a_nested_1h_mark_after_the_boundary_sets_the_ttl_not_the_later_5m_end_mark():
    # Codex review r1: the donor scan skipped a mark inside an edited tool result and copied the final 5m mark, giving
    # 1h, 5m, 1h, 5m, which the API rejects (TTLs must not increase).
    old, new = shape(302, 306, 330, 330)
    new['messages'][-1]['content'][-1]['cache_control'] = {'type': 'ephemeral', 'ttl': '5m'}
    new['messages'][310]['content'][0]['content'][0]['cache_control'] = dict(MARK)
    out = json.loads(admission.mark_last_equal_row(gate_for(old), json.dumps(new).encode()))
    assert out['messages'][305]['content'][-1]['cache_control'] == MARK
    # Session.17's donor (setting off) is unchanged.
    assert admission._donor_mark(new, 305, 1) == {'type': 'ephemeral', 'ttl': '5m'}


def test_an_image_added_after_the_equal_prefix_changes_the_identity():
    # Codex review r1: adding the first image anywhere invalidates the cached message prefix.
    old, new = shape(302, 306, 330, 330)
    with_image = json.loads(json.dumps(new))
    with_image['messages'][-1]['content'].insert(0, {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AAAA'}})
    gate = gate_for(old)
    data = json.dumps(with_image).encode()
    assert admission.mark_last_equal_row(gate, data) is data
    assert gate.boundary_note == ' boundary=-:identity'
    nested = json.loads(json.dumps(new))
    nested['messages'][320]['content'][0]['content'].append({'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AAAA'}})
    assert admission.request_identity(nested) != admission.request_identity(new)


def test_the_note_is_set_only_after_the_payload_is_built():
    old, new = shape(302, 306, 330, 330)
    # Codex review r1 nit: a payload that cannot be re-encoded (a lone surrogate in a later row) is forwarded as is,
    # and the note does not claim a mark.
    text = json.dumps(new).replace('"step 329"', '"\\ud800 step 329"', 1)
    assert text != json.dumps(new)
    gate = gate_for(old)
    data = text.encode()
    assert admission.mark_last_equal_row(gate, data) is data
    assert gate.boundary_note == ' boundary=-:error'
