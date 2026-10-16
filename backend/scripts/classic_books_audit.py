"""Opt-in live ingestion/preparation audit of frozen public-domain chapters and questions.

No existing library is opened. Every result uses natural provider extraction and the production
answer/recap gates. Semantic accuracy requires comparison with the frozen references, not HTTP 200.
"""
import argparse
from copy import deepcopy
from html import escape
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app.answer_bank import valid_answer
from app.config import Settings
from app.ingest.manifest import load_manifest
from app.main import create_app
from app.reader_prepare import PreparationScope, prepare
from tests.api._epub import epub_ncx
from scripts.real_book_audit import CANARY, install_observers, restore_rows, latency


BOOKS = {
    11: ("Alice's Adventures in Wonderland", {
        1: [
            ("What was in the jar labelled orange marmalade?", "The jar was empty."),
            ("How tall was Alice after drinking from the little bottle?", "Ten inches high."),
            ("Why could Alice not enter the garden after shrinking?", "She left the golden key on the glass table, could not reach it, and could not climb its slippery leg."),
            ("What about the White Rabbit made Alice run after it?", "Its watch and waistcoat-pocket aroused her curiosity."),
        ],
        2: [
            ("What did the White Rabbit drop when Alice frightened him?", "White kid gloves and a fan."),
            ("Why did Alice drop the fan?", "She realized it was making her shrink and dropped it to avoid shrinking away."),
            ("What animal did Alice speak to while swimming in the pool?", "A Mouse."),
        ],
    }, "Who wins the Caucus-race and who supplies the prizes?"),
    1342: ("Pride and Prejudice", {
        1: [
            ("Which estate has Mr Bingley rented?", "Netherfield Park."),
            ("What yearly income does Mrs Bennet report for Mr Bingley?", "Four or five thousand pounds a year."),
            ("Why does Mrs Bennet want her husband to visit Mr Bingley?", "She hopes Bingley will marry one of their daughters."),
            ("Which daughter does Mr Bennet say has more quickness than her sisters?", "Lizzy."),
        ],
        2: [
            ("Had Mr Bennet already visited Mr Bingley before telling his family?", "Yes, he was among the earliest visitors and disclosed it only that evening."),
            ("What does Mr Bennet tell Kitty about coughing after revealing his visit?", "Now Kitty may cough as much as she chooses."),
            ("What does Mrs Bennet say about Lydia's height?", "Though the youngest, she is the tallest."),
        ],
    }, "What does Darcy say about Elizabeth when Bingley suggests dancing with her?"),
    1661: ("A Scandal in Bohemia", {
        1: [
            ("Who visits Sherlock Holmes while wearing a mask?", "The King of Bohemia, initially calling himself Count von Kramm."),
            ("What evidence does Irene Adler possess that worries the King?", "A photograph of Irene Adler and the King together."),
            ("What does Holmes infer from the six scratches on Watson's left shoe?", "A careless servant scraped mud from the shoe; Holmes says Watson has been out in wet weather and has a careless servant."),
        ],
    }, "Where in Irene Adler's house is the photograph concealed?"),
}


class InlineExecutor:
    def submit(self, function, *args, **kwargs):
        function(*args, **kwargs)

    def shutdown(self, wait=True):
        pass


def chapters(book, text):
    if book == 1661:
        start = re.search(r"(?m)^I\.\s*$", text).start()
        second = re.search(r"(?m)^II\.\s*$", text[start:]).start()+start
        third = re.search(r"(?m)^III\.\s*$", text[second:]).start()+second
        end = text.index("II. THE RED-HEADED LEAGUE", third)
        return [text[start:second], text[second:third], text[third:end]]
    matches = list(re.finditer(r"(?m)^CHAPTER [IVX]+\.\s*$", text))
    if book == 1342:
        start = text.index("It is a truth universally acknowledged")
        bounds = [start, *[m.start() for m in matches if m.start() > start]]
    else:
        bounds = [m.start() for m in matches]
    if len(bounds) < 4:
        raise ValueError("public-domain chapter boundaries changed")
    return [text[bounds[i]:bounds[i+1]] for i in range(3)]


def summarize(report):
    stages = [s for b in report['books'] for s in b['checkpoints']]
    probes = [p for s in stages for p in s['probes']]
    requested = [p for p in probes if p['expected'] == 'answer']
    answered = [p for p in requested if p['status'] == 200 and p.get('valid_shape') is True]
    future = [p for p in probes if p['expected'] == 'refuse']
    return {'books': len(report['books']), 'questions': len(probes), 'answered': len(answered),
        'all_answer_request_latency': latency(requested), 'answer_latency': latency(answered),
        'prepared_reuses': sum(p['value'].get('cached', False) for p in answered),
        'future_questions': len(future),
        'future_refusals': sum(p['status'] == 200 and p['value'].get('insufficient_evidence') is True
                               and not p['value'].get('claims') for p in future),
        'ingestion_failures': sum(s['ingest']['ingest_progress'] < s['frontier'] for s in stages),
        'provider_canary_leaks': sum(c['unread_canary'] for c in report['provider_calls']),
        'usd': sum(b['usd'] for b in report['books']),
        'semantic_accuracy': 'Requires manual primary-source comparison; HTTP success is not proof.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--books", type=int, nargs="+", choices=BOOKS, default=list(BOOKS))
    args = parser.parse_args()
    if not args.run:
        parser.error("--run is required; at most $0.25 per selected book")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"scope": "Natural ingestion and complete independent questions on public-domain chapters.",
              "books": [], "provider_calls": []}
    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    for book in args.books:
        title, cases, future = BOOKS[book]
        source_url = f"https://www.gutenberg.org/cache/epub/{book}/pg{book}.txt"
        if args.source_dir:
            raw = (args.source_dir/f"pg{book}.txt").read_bytes()
        else:
            with urlopen(source_url, timeout=30) as response:  # fixed public-domain source
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("source download exceeded its bound")
        texts = chapters(book, raw.decode("utf-8-sig").replace("\r\n", "\n"))
        row = {"title": title, "source": source_url, "source_sha256": hashlib.sha256(raw).hexdigest(),
               "chapter_sha256": [hashlib.sha256(t.encode()).hexdigest() for t in texts], "checkpoints": []}
        report["books"].append(row)
        # The full unread chapter is stored, but neither its facts nor this marker may reach a provider.
        for unread_index in range(max(cases), len(texts)):
            texts[unread_index] += "\n" + CANARY
        with tempfile.TemporaryDirectory(prefix="classic-audit-", dir=args.output.parent) as scratch:
            settings = Settings(_env_file=str(args.env_file.resolve()), data_dir=str(Path(scratch)/'data'),
                frontend_dist_dir=None, deployment_mode="local", trusted_hosts="testserver",
                jev_enabled=True, jev_windows_credential=True, jev_selection_enabled=True,
                jev_evidence_enabled=True, jev_partial_evidence_enabled=True,
                jev_extraction_enabled=True, jev_identity_enabled=True,
                reader_answer_cache=True, reader_reuse_enabled=True, recap_disk_cache=True,
                reader_prepare_enabled=False, recap_prewarm=False, reader_fast_mode=False,
                reader_prepare_max_usd_per_book=.15, cost_max_usd_per_book=.25)
            app = create_app(settings, ingest_executor=InlineExecutor())
            with TestClient(app) as api:
                phase = {"book": book, "kind": "ingestion", "frontier": 0}
                install_observers(app.state.reader_client, report, phase)
                original_complete = app.state.reader_client._complete_openai
                def observe_names(system, user, tier, schema, max_output_tokens):
                    value, usage = original_complete(system, user, tier, schema, max_output_tokens)
                    if getattr(schema, "__name__", None) == "Extraction":
                        report["provider_calls"][-1]["entity_names"] = [
                            {"name": e['canonical_name'], "aliases": e.get('aliases', [])}
                            for e in value.get('entities', [])]
                    if getattr(schema, "__name__", None) == "ExtractionVerdict":
                        report["provider_calls"][-1]["review_rejections"] = {
                            k: value[k] for k in ('references_future', 'unsupported_claims')}
                    return value, usage
                app.state.reader_client._complete_openai = observe_names
                epub = epub_ncx([(f"c{i}.xhtml", f"Chapter {i}", f"Chapter {i}", escape(t))
                                 for i, t in enumerate(texts, 1)], title=title, language="en")
                imported = api.post('/api/books', files={'file':('classic.epub',epub,'application/epub+zip')})
                imported.raise_for_status()
                bid = imported.json()['book_id']
                atoms = load_manifest(settings.data_dir, bid)['atoms']
                if len(atoms) != 3:
                    raise ValueError("chapter segmentation changed")
                for frontier, questions in cases.items():
                    phase.update(kind="ingestion", frontier=frontier)
                    stage = {"frontier":frontier,"probes":[]}
                    row['checkpoints'].append(stage)
                    settings.reader_prepare_enabled = settings.recap_prewarm = False
                    api.put(f'/api/books/{bid}/position', json={'cfi':f'audit-{frontier}',
                        'offset':sum(a['char_len'] for a in atoms[:frontier]), 'position_epoch':0}).raise_for_status()
                    stage['ingest'] = api.get(f'/api/books/{bid}/ingest').json()
                    save()
                    print(f"{title} chapter {frontier}: ingestion {stage['ingest']['status']}", flush=True)
                    if stage['ingest']['ingest_progress'] < frontier:
                        break
                    phase.update(kind="preparation")
                    settings.reader_prepare_enabled = settings.recap_prewarm = True
                    started = time.perf_counter()
                    try:
                        prepare(app, PreparationScope.capture(app, bid, app.state.catalog.get_book(bid)['incarnation']))
                    except Exception as error:
                        stage['preparation_error'] = type(error).__name__
                    stage['preparation_seconds'] = time.perf_counter()-started
                    stage['suggestions'] = api.get(f'/api/books/{bid}/prepared-questions').json()['questions']
                    bank = app.state.reader_client.answers
                    prepared_rows = deepcopy(bank.rows)
                    phase.update(kind="probe")
                    for question, reference in [*questions, (future, 'The requested scene is unread; refuse.')]:
                        restore_rows(bank, prepared_rows)
                        started = time.perf_counter()
                        reply = api.post(f'/api/books/{bid}/ask', json={'question':question,'bookmark':frontier})
                        value = reply.json()
                        stage['probes'].append({'question':question,'reference':reference,
                            'expected':'refuse' if question == future else 'answer',
                            'seconds':time.perf_counter()-started,'status':reply.status_code,
                            'valid_shape':valid_answer(value) if value.get('claims') else None,'value':value})
                        save()
                    restore_rows(bank, prepared_rows)
                    phase.update(kind="recap")
                    started = time.perf_counter()
                    reply = api.get(f'/api/books/{bid}/catch-me-up')
                    stage['recap'] = {'status':reply.status_code,'seconds':time.perf_counter()-started,'value':reply.json()}
                    stage['usd'] = app.state.catalog.total_cost(bid)
                    print(f"{title} chapter {frontier}: {len(stage['suggestions'])} suggestions; recap {reply.status_code}", flush=True)
                    save()
                row['usd'] = app.state.catalog.total_cost(bid)
                row['pending'] = len(app.state.catalog.get_cost_reservations(bid))
                api.post(f'/api/books/{bid}/position/reset', json={'position_epoch':0}).raise_for_status()
                response = api.post(f'/api/books/{bid}/ask', json={'question':'What happens next?','bookmark':999}).json()
                row['reset_safe'] = response.get('as_of_chapter') == 0 and not response.get('claims')
            settings.reader_prepare_enabled = settings.recap_prewarm = False
            with TestClient(create_app(settings)) as api:
                row['restart_reset_safe'] = api.get(f'/api/books/{bid}/prepared-questions').json() == {'as_of_chapter':0,'questions':[]}
        save()
    report['summary'] = summarize(report)
    save()
    print(json.dumps(report['summary']), flush=True)
    result = report['summary']
    return int(result['ingestion_failures'] or result['provider_canary_leaks']
        or result['future_refusals'] != result['future_questions']
        or result['answered'] != result['questions']-result['future_questions'])


if __name__ == '__main__':
    raise SystemExit(main())
