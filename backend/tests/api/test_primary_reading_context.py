"""Adversarial source, temporal-context and complete-answer reuse regressions; no network."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.ask import AskDraft, ask_judge_prompt, retrieve_ask_sources
from app.ask_evidence import AskEvidenceError, complete_evidence_citations, evidence_prompt
from app.eval.spoiler_gate.judge import JudgeUnavailable
from app.llm.jev import JevUnavailable
from app.llm.jev_tasks import reuse_answer
from app.config import Settings
from app.llm.jev_budget import estimate_jev_input
from app.llm import jev_tasks
from app.preparation_plan import preparation_facts
from app.recap_grounding import primary_generation_prompt, primary_snapshot, recap_evidence, review_primary_recap
from test_reader_preparation import payload
from test_views import env as env, _supported_citation_audit


def test_primary_snapshot_and_plan_never_read_an_uncompleted_raw_chapter(env):
    _api, bid, app, _atoms = env
    with app.state.store.book(bid) as mem:
        chapters, raw = primary_snapshot(mem, 1)
        assert chapters and all(r["revealed_at"] == 1 for r in chapters)
        assert "Berenice" not in json.dumps(raw) and "Corvus" not in json.dumps(raw)
        facts = preparation_facts(mem, 2)
        primary = [r for r in facts if r["kind"] == "source"]
        assert primary and "Berenice" in json.dumps(primary)
        assert "Corvus" not in json.dumps(facts)
        text = " ".join(mem.view(2).raw_text(chapters[0]["chapter_key"]).split())
        assert text  # source came through the visible raw-text funnel


def test_recap_generation_uses_raw_text_instead_of_false_legacy_summary():
    snapshot = ([
        {'revealed_at': 1, 'chapter_key': 'one', 'summary_hint': 'FALSE_LEGACY_FACT'},
        {'revealed_at': 2, 'chapter_key': 'two', 'summary_hint': 'Nell inherited everything at twenty.'},
        {'revealed_at': 3, 'chapter_key': 'three', 'summary_hint': 'UNREAD_CANARY'},
    ], {'one': 'Nell first received money at twenty.',
        'two': 'Nell walked home. ' * 150 + 'Four years later Nell discovered that the estate was empty.',
        'three': 'UNREAD_CANARY'})
    prompt = primary_generation_prompt(snapshot, 2, {})
    sources = json.loads(prompt.split('(untrusted JSON data):\n')[1])
    assert 'FALSE_LEGACY_FACT' not in prompt and 'UNREAD_CANARY' not in prompt
    assert 'inherited everything' not in prompt and 'Four years later' in prompt
    assert all(1 <= s['completed_chapter'] <= 2 for s in sources)
    for source in sources:
        raw = snapshot[1]['one' if source['completed_chapter'] == 1 else 'two']
        assert source['text'] in ' '.join(raw.split())


@pytest.mark.parametrize('limit', [1, 500])
def test_recap_generation_does_not_truncate_primary_evidence_to_fit(limit):
    snapshot = ([{'revealed_at': 1, 'chapter_key': 'one'}], {'one': 'Nell carried a box. ' * 100})
    with pytest.raises(JudgeUnavailable):
        primary_generation_prompt(snapshot, 1, {}, byte_limit=limit)


def test_plan_keeps_chapter_ending_details_missing_from_legacy_summary(env):
    _api, bid, app, atoms = env
    with app.state.store.book(bid) as mem:
        key = atoms[1]["key"]
        # This is a test-only primary-text correction, never a production data migration.
        with mem._writer():
            mem._conn.execute("UPDATE raw_chapters SET text=? WHERE chapter_key=?", (
                "Berenice met Aldric. " + "The wind crossed the valley. " * 300
                + "Berenice put the cobalt key in a clay cup.", key,
            ))
        facts = preparation_facts(mem, 2)
        assert any(f["kind"] == "source" and "clay cup" in f["text"] for f in facts)
        assert any(f["kind"] == "character" for f in facts)
        assert len(json.dumps(facts, ensure_ascii=False).encode()) <= 18000


def test_repeated_scene_current_candidates_survive_high_overlap_older_scenes():
    chapters = [{"revealed_at": i, "chapter_key": str(i)} for i in range(1, 31)]
    raw = {str(i): "Lise told Alyosha what her mother said. " * 100 for i in range(1, 30)}
    raw['30'] = "Lise listened to the conversation. Alyosha put the note in his pocket and went downstairs."
    sources = retrieve_ask_sources("Does Alyosha return to her mother after leaving Lise?",
        bookmark=30, chapters=chapters, read_chapter=raw.get, semantic_hits=(), atoms={},
        candidate_pool=True, known_names=['Alyosha', 'Lise'])
    assert len(sources) <= 24 and any(s['ordinal'] == 30 for s in sources)
    current = next(s for s in sources if s['ordinal'] == 30)
    assert current['text'] == raw['30']
    older = next(s for s in sources if s['ordinal'] < 30)
    draft = AskDraft.model_validate({'insufficient_evidence': False, 'claims': [
        {'text': 'Lise told Alyosha what her mother said.', 'citation_ids': [older['id']]}]})
    prompt = ask_judge_prompt(draft, [older], question='What did Lise say?',
                             frontier=30, scene_sources=[current])
    data = json.loads(prompt.split('(untrusted JSON data):\n')[1])
    assert data['latest_completed_chapter'] == 30
    assert data['claims'][0]['cited_passages'][0]['completed_chapter'] < 30
    assert data['current_scene_context'][0]['text'] == raw['30']
    assert current['text'] not in json.dumps(data['claims'])


@pytest.mark.parametrize('fault', [None, 'missing', 'foreign', 'small_budget'])
def test_source_selection_batches_preserve_all_exact_candidates_and_hard_input_limits(fault):
    sources = [dict(id=i, ordinal=i, chapter_key=str(i), title=f'Chapter {i}',
                    text=(f'Nell carried parcel {i}. ' * 80)[:1500]) for i in range(1, 25)]
    original, seen, calls = deepcopy(sources), [], []
    settings = Settings(_env_file=None, cost_max_input_tokens_per_call=1000 if fault == 'small_budget' else 60000)
    def run(state, questions):
        assert state['latest_completed_chapter'] == 24
        assert estimate_jev_input(settings.jev_model, state, questions) <= 30000
        assert all(s['text'] == sources[s['id']-1]['text'] for s in state['candidates'])
        seen.extend(s['id'] for s in state['candidates'])
        calls.append(state)
        reply = {k: {'choice': 'direct', 'confidence': 1.0,
                      'probabilities': {'direct': .99, 'context': .01, 'irrelevant': 0.0}} for k in questions}
        if fault == 'missing':
            reply.pop(next(iter(reply)))
        if fault == 'foreign':
            reply['99'] = next(iter(reply.values()))
        return reply
    if fault:
        with pytest.raises(JevUnavailable):
            jev_tasks.select('What did Nell carry?', sources, run=run, settings=settings, frontier=24)
        assert not calls if fault == 'small_budget' else len(calls) == 1
    else:
        selected = jev_tasks.select('What did Nell carry?', sources, run=run, settings=settings, frontier=24)
        assert 1 < len(calls) <= 4 and seen == list(range(1, 25))
        assert selected and all(s == sources[s['id']-1] for s in selected)
    assert sources == original


@pytest.mark.parametrize('fault', ['unsupported', 'missing', 'foreign_span', 'false_final', 'outage', None])
def test_primary_recap_requires_complete_unit_proof_and_independent_own_citation_review(monkeypatch, fault):
    from app import recap_grounding as module
    snapshot = ([{'revealed_at': 2, 'chapter_key': 'two'}], {
        'two': 'Nell first received money at twenty. Four years later Nell learned the estate was empty.'})
    text = 'Four years after receiving money, Nell learned the estate was empty.'
    calls = []
    def complete(*args, **kwargs):
        data = json.loads(kwargs['user'].split('(untrusted JSON data):\n')[1])
        calls.append(kwargs['schema'].__name__)
        if kwargs['schema'].__name__ == 'AskEvidenceReview':
            reply = _supported_citation_audit(data, [data['sources'][0]['source_id']])
            if fault == 'missing':
                reply['claim_audits'][0]['units'].pop()
            if fault == 'foreign_span':
                reply['claim_audits'][0]['units'][0]['evidence'][0]['passage_ids'] = [999]
            if fault == 'unsupported':
                reply['claim_audits'][0]['units'][0].update(supported=False, evidence=[])
        else:
            assert calls == ['AskEvidenceReview', 'AskVerdict']
            assert data['claims'][0]['claim'] == text
            assert 'Four years later' in data['claims'][0]['cited_passages'][0]['text']
            reply = {'references_future': False, 'unsupported_claims': ['chronology'] if fault == 'false_final' else []}
        if fault == 'outage':
            raise RuntimeError('private provider details')
        return SimpleNamespace(value=reply)
    monkeypatch.setattr(module, 'budgeted_completion', complete)
    args = dict(catalog=None, settings=None, client=None, book_id='b')
    if fault:
        with pytest.raises(JudgeUnavailable):
            review_primary_recap(text, snapshot, 2, {}, **args)
        assert len(calls) == (2 if fault == 'false_final' else 1)
    else:
        now, sources = review_primary_recap(text, snapshot, 2, {}, **args)
        assert now == text and sources[0]['ordinal'] == 2


@pytest.mark.parametrize('text', ['', 'x' * 4801, 'Nell worked. ' * 7, 'x' * 801])
def test_recap_never_drops_unreviewable_text_to_make_a_pass(text):
    with pytest.raises(JudgeUnavailable):
        recap_evidence(text, ([], {}), 1, {})


def test_primary_recap_keeps_default_ask_source_limit():
    sources = [dict(id=i, ordinal=i, chapter_key=str(i), title='', text='Nell worked.') for i in range(1, 8)]
    draft = {'insufficient_evidence': False, 'claims': [{'text': 'Nell worked.', 'citation_ids': [1]}]}
    with pytest.raises(AskEvidenceError):
        evidence_prompt(draft, sources)
    data = json.loads(evidence_prompt(draft, sources[:6]).split('(untrusted JSON data):\n')[1])
    good = _supported_citation_audit(data, [1])
    assert complete_evidence_citations(draft, sources[:6], good) == draft


@pytest.mark.parametrize('reject_last', [False, True])
def test_recap_proof_batches_cover_every_sentence_before_final_judge(monkeypatch, reject_last):
    from app import recap_grounding as module
    sentences = ['Nell carried a box.', 'Orin repaired a boat.', 'Dena opened the gate.']
    snapshot = ([{'revealed_at': 2, 'chapter_key': 'two'}], {'two': ' '.join(sentences)})
    reviewed, final = [], []
    def complete(*args, **kwargs):
        data = json.loads(kwargs['user'].split('(untrusted JSON data):\n')[1])
        if kwargs['schema'].__name__ == 'AskEvidenceReview':
            assert 1 <= len(data['claims']) <= 3 and len(data['sources']) <= 6
            reviewed.extend(c['text'] for c in data['claims'])
            reply = _supported_citation_audit(data, [data['sources'][0]['source_id']])
            if reject_last and len(reviewed) == 3:
                reply['claim_audits'].clear()
        else:
            final.extend(c['claim'] for c in data['claims'])
            reply = {'references_future': False, 'unsupported_claims': []}
        return SimpleNamespace(value=reply)
    monkeypatch.setattr(module, 'budgeted_completion', complete)
    args = dict(catalog=None, settings=None, client=None, book_id='b')
    if reject_last:
        with pytest.raises(JudgeUnavailable):
            review_primary_recap(' '.join(sentences), snapshot, 2, {}, **args)
        assert not final
    else:
        review_primary_recap(' '.join(sentences), snapshot, 2, {}, **args)
        assert final == sentences
    assert reviewed == sentences


@pytest.mark.parametrize('mode', ['pass', 'outage', 'false_selection'])
def test_jev_recap_selection_never_clears_primary_claims_or_supplies_future_text(monkeypatch, mode):
    from app import recap_grounding as module
    snapshot = ([{'revealed_at': i, 'chapter_key': str(i)} for i in (1, 2, 3)], {
        '1': 'Nell carried a box.', '2': 'Nell carried a bag.', '3': 'UNREAD_CANARY'})
    calls, selections = [], []
    def typed(*args, **kwargs):
        assert kwargs['phase'] == 'jev-recap-source-selection'
        state = kwargs['state']
        assert 'UNREAD_CANARY' not in json.dumps(state)
        assert all(s['ordinal'] <= 2 for s in state['candidates'])
        selections.append(state)
        if mode == 'outage':
            raise JevUnavailable('unavailable')
        match = 'bag' if mode == 'false_selection' else 'box'
        return SimpleNamespace(value={str(s['id']): {
            'choice': 'direct' if match in s['text'] else 'irrelevant', 'confidence': 1.0,
            'probabilities': {'direct': float(match in s['text']), 'context': 0.,
                              'irrelevant': float(match not in s['text'])}}
            for s in state['candidates']})
    def complete(*args, **kwargs):
        data = json.loads(kwargs['user'].split('(untrusted JSON data):\n')[1])
        assert 'UNREAD_CANARY' not in kwargs['user']
        calls.append(kwargs['schema'].__name__)
        if calls[-1] == 'AskEvidenceReview':
            match = 'bag' if mode == 'false_selection' else 'box'
            source_id = next(s['source_id'] for s in data['sources']
                             if any(match in p['text'] for p in s['passages']))
            return SimpleNamespace(value=_supported_citation_audit(data, [source_id]))
        assert calls == ['AskEvidenceReview', 'AskVerdict']
        assert all(('bag' if mode == 'false_selection' else 'box') in s['text']
                   for s in data['claims'][0]['cited_passages'])
        return SimpleNamespace(value={'references_future': False,
            'unsupported_claims': ['wrong object'] if mode == 'false_selection' else []})
    monkeypatch.setattr(module, 'budgeted_jev', typed)
    monkeypatch.setattr(module, 'budgeted_completion', complete)
    args = dict(catalog=None, settings=Settings(_env_file=None),
                client=SimpleNamespace(jev=SimpleNamespace(model='jev-1.13.0')), book_id='b')
    if mode == 'false_selection':
        with pytest.raises(JudgeUnavailable):
            review_primary_recap('Nell carried a box.', snapshot, 2, {}, **args)
    else:
        review_primary_recap('Nell carried a box.', snapshot, 2, {}, **args)
    assert selections and calls == ['AskEvidenceReview', 'AskVerdict']


@pytest.mark.parametrize('mode', ['pass', 'reject', 'repair', 'malformed', 'outage'])
def test_real_provider_recap_gate_cache_and_generic_failures(env, monkeypatch, mode):
    from app.api import views
    api, bid, app, _atoms = env
    client = app.state.reader_client
    client.provider = 'openai-compatible'
    client.cheap_model = client.large_model = 'gpt-6-luna'
    # A legacy-memory lexical mismatch cannot veto primary proof, but it cannot authorize
    # anything either: every mode below still requires complete proof and an own-citation judge.
    safe_score = {'future_entity_leaks': [], 'prolepsis_hits': [],
                  'unsupported_event_bindings': [], 'ungrounded_sentences': ['legacy mismatch']}
    monkeypatch.setattr(views, 'score_recap', lambda *a, **kw: safe_score)
    text = 'Berenice met Aldric at the forge and they spoke.'
    calls, generations = [], []
    def complete(system, user, tier='cheap', schema=None):
        assert not app.state.store._lock_for(bid).locked()
        assert 'Corvus' not in user
        calls.append(getattr(schema, '__name__', 'synthesis'))
        if schema is None:
            generations.append(user)
            return text, {'in': 50, 'out': 15}
        data = json.loads(user.split('(untrusted JSON data):\n')[1])
        if schema.__name__ == 'AskEvidenceReview':
            assert all(s['completed_chapter'] <= 2 for s in data['sources'])
            assert any('Berenice met Aldric' in p['text'] for s in data['sources'] for p in s['passages'])
            if mode == 'outage':
                raise RuntimeError('private-secret')
            value = _supported_citation_audit(data, [next(s['source_id'] for s in data['sources'] if s['completed_chapter'] == 2)])
            if mode == 'malformed':
                value['claim_audits'].clear()
            return value, {'in': 50, 'out': 50}
        assert schema.__name__ == 'AskVerdict'
        assert data['claims'][0]['claim'] == text
        reject = mode == 'reject' or mode == 'repair' and len(generations) == 1
        return {'references_future': False, 'unsupported_claims': ['wrong timing'] if reject else []}, {'in': 50, 'out': 10}
    monkeypatch.setattr(client, 'complete', complete)
    response = api.get(f'/api/books/{bid}/catch-me-up?bookmark=99')
    passed = mode in {'pass', 'repair'}
    assert response.status_code == (200 if passed else 502), response.text
    assert 'private-secret' not in response.text and 'wrong timing' not in response.text
    if passed:
        assert response.json()['recap'] == text and response.json()['as_of_chapter'] == 2
        if mode == 'repair':
            assert 'PRIMARY SOURCE CORRECTION' in generations[1]
        count = len(calls)
        assert api.get(f'/api/books/{bid}/catch-me-up').json()['cached'] is True
        assert len(calls) == count
        if mode == 'pass':
            for name, value in [('cheap_model', 'gpt-6.1-sol'), ('base_url', 'https://example.invalid/v1')]:
                setattr(client, name, value)
                refreshed = api.get(f'/api/books/{bid}/catch-me-up')
                assert refreshed.status_code == 200 and refreshed.json()['cached'] is False
                assert len(calls) == count + 3  # synthesis, positive proof, independent judge
                count = len(calls)
            for gate in ('future_entity_leaks', 'prolepsis_hits', 'unsupported_event_bindings'):
                score = {**safe_score, gate: ['private-canary']}
                monkeypatch.setattr(views, 'score_recap', lambda *a, _score=score, **kw: _score)
                client.base_url += '/changed'
                before = len(calls)
                blocked = api.get(f'/api/books/{bid}/catch-me-up')
                assert blocked.status_code == 502 and 'private-canary' not in blocked.text
                assert calls[before:] == ['synthesis', 'synthesis']
    else:
        assert app.state.recaps.stats()['entries'] == 0
    assert app.state.catalog.get_cost_reservations(bid) == []


@pytest.mark.parametrize('fault', [None, 'premise', 'coverage', 'older_scene', 'newer_chapter'])
def test_reuse_subset_requires_compatible_premises_complete_coverage_and_current_scene(fault):
    value = payload()
    value['claims'][0]['text'] = 'Berenice met Aldric at the forge before noon.'
    candidate = {'reviewed_question': 'Describe the meeting with its place and time.', 'payload': value}
    before = deepcopy(candidate)
    query = 'Where did Berenice meet Aldric?'
    if fault == 'older_scene':
        value['citations'][0]['ordinal'] = 1
    if fault == 'newer_chapter':
        query = 'Where did Berenice meet Aldric in chapter 3?'
    def run(state, questions):
        assert 'latest_completed_chapter' in state['candidates'][0]
        answers = {k: {'choice': 'yes', 'probabilities': {'yes': 1.0}, 'confidence': 1.0} for k in questions}
        if fault in {'premise', 'coverage'}:
            answers['q0' if fault == 'premise' else 'a0']['choice'] = 'no'
        return answers
    args = dict(run=run, settings=SimpleNamespace(reader_reuse_min_probability=.99, reader_reuse_min_confidence=.9))
    if fault:
        with pytest.raises(JevUnavailable):
            reuse_answer(query, [candidate], **args)
    else:
        assert reuse_answer(query, [candidate], **args) is candidate
        assert candidate == before
