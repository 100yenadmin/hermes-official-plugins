"""Reasoning off. An auxiliary one-shot (title, compression) keeps the nearest real "off": the disable where the
route accepts it, else the lowest effort, never native's default. A conversation step keeps thinking on at the lowest
effort instead, so a one-turn "off" (Hermes' voice-turn route) never toggles the thinking parameters, which would drop
the conversation's message cache."""
import json

import directsdk


def reasoning_off_body(model, reasoning, auxiliary=True):
    return json.loads(directsdk.request_body({
        'model': model, 'messages': [{'role': 'user', 'content': 'go'}],
        'extra_body': {'reasoning': reasoning}, **({'hermes_auxiliary': True} if auxiliary else {})})[0])


def test_reasoning_off_asks_for_the_lowest_effort_where_thinking_cannot_be_disabled():
    # These routes 400 on the disable, so "off" is impossible; with no effort native thinks at its
    # default (measured: 600-1200 reasoning tokens on a short structured answer, ~2x the latency).
    for route in ('haiku', 'claude-haiku-5-5[1m]', 'sonnet', 'claude-sonnet-5-5', 'opus', 'claude-opus-5-5', 'fable',
                  'claude-fable-5-2', 'claude-fable-5-2[1m]', 'claude-mythos-5-2', 'claude-mythos-5-2[1m]',
                  'claude-mythos-5-1', 'claude-mythos-5-1[1m]', 'claude-mythos-5'):
        for reasoning in ({'enabled': False}, {'effort': 'none'}):
            body = reasoning_off_body(route, reasoning)
            assert 'thinking' not in body, route
            assert body['output_config'] == {'effort': 'low'}, route


def test_reasoning_off_keeps_the_real_disable_where_it_is_accepted():
    for route in ('claude-sonnet-5', 'claude-opus-5', 'claude-haiku-4-5'):
        body = reasoning_off_body(route, {'enabled': False})
        assert body['thinking'] == {'type': 'disabled'}, route
        assert 'output_config' not in body, route


def test_reasoning_off_effort_coexists_with_a_structured_output_format():
    fmt = {'type': 'json_schema', 'json_schema': {'name': 'o', 'schema': {'type': 'object', 'properties': {}}}}
    for auxiliary in ({'hermes_auxiliary': True}, {}):
        body = json.loads(directsdk.request_body({
            'model': 'haiku', 'messages': [{'role': 'user', 'content': 'go'}], 'response_format': fmt,
            'extra_body': {'reasoning': {'enabled': False}}, **auxiliary})[0])
        assert body['output_config']['effort'] == 'low'
        assert body['output_config']['format']['type'] == 'json_schema'


ADAPTIVE = ('haiku', 'claude-haiku-5-5[1m]', 'sonnet', 'claude-sonnet-5-5', 'opus', 'claude-opus-5-5',
            'claude-opus-5-5[1m]', 'fable', 'claude-fable-5-2', 'claude-mythos-5-2[1m]', 'claude-sonnet-5', 'claude-opus-5')


def test_a_conversation_step_keeps_thinking_on_at_the_lowest_effort_when_reasoning_is_off():
    for route in ADAPTIVE:
        on = reasoning_off_body(route, {'enabled': True, 'effort': 'high'}, auxiliary=False)
        for reasoning in ({'enabled': False}, {'effort': 'none'}, {'enabled': True, 'effort': 'none'}):
            off = reasoning_off_body(route, reasoning, auxiliary=False)
            # Identical thinking parameters, only the effort differs: the message cache survives the turn.
            assert off['thinking'] == on['thinking'] == {'type': 'adaptive', 'display': 'summarized'}, route
            assert off['output_config'] == {'effort': 'low'}, route
            assert 'context_management' not in off, route


def test_a_conversation_step_on_a_route_without_adaptive_thinking_keeps_the_disable():
    body = reasoning_off_body('claude-haiku-4-5', {'enabled': False}, auxiliary=False)
    assert body['thinking'] == {'type': 'disabled'}


def test_reasoning_enabled_effort_is_unchanged():
    body = reasoning_off_body('haiku', {'enabled': True, 'effort': 'high'})
    assert body['thinking'] == {'type': 'adaptive', 'display': 'summarized'}
    assert body['output_config'] == {'effort': 'high'}
