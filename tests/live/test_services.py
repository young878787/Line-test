"""有界真實 CWA/BIRC 驗收；LINE 永遠使用 mock，不使用 LINE 金鑰。"""
import asyncio
from contextlib import contextmanager
from datetime import timedelta
import json
from pathlib import Path
import time

import pytest
import httpx
from pydantic import ValidationError

from conftest import LineMock, chat
from weather_bot.ai import AIClient, AIOutputError, PROMPT_VERSION
from weather_bot.config import Clock, Settings
from weather_bot.schemas import Reply
from weather_bot.workers import Bot

pytestmark = pytest.mark.live


class BoundedAI:
    def __init__(self, client):
        self.client, self.calls = client, []

    async def generate(self, system, data, *, memory=False):
        if len(self.calls) >= 8:
            raise ValueError('live call budget exhausted')
        entry = {'memory': memory, 'mode': data.get('mode'), 'compact': data.get('compact'), 'input': data}
        self.calls.append(entry)
        start = time.monotonic()
        try:
            output = await self.client.generate(system, data, memory=memory)
            entry['output'] = output
            return output
        except Exception as exc:
            entry['error_type'] = type(exc).__name__
            if hasattr(exc, 'raw_output'):
                entry['raw_output'] = exc.raw_output
            raise
        finally:
            entry['duration_seconds'] = time.monotonic() - start


async def test_live_T01_T05_end_to_end(ci_database_path):
    settings = Settings.from_env()
    path = Path('logs/live/latest.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    results = {key: {'status': 'NOT_RUN'} for key in ('T01', 'T02', 'T03', 'T04', 'T05')}
    report = {'model': settings.ai_model, 'prompt_version': PROMPT_VERSION, 'line_delivery': 'MOCK_ONLY', 'results': results}
    if not settings.cwa_key or not settings.ai_key:
        report['reason'] = 'Missing CWA_API_KEY or AI_API_Keys'
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        pytest.skip('NOT_RUN: missing CWA/BIRC credentials')
    settings.database = ci_database_path
    clock = Clock()
    line = LineMock(clock)
    bot = Bot(settings, clock=clock, sender=line)
    model = BoundedAI(AIClient(settings, bot.http))
    bot.ai = bot.memory.ai = bot.chat.ai = model
    bot.line = line

    @contextmanager
    def check(case):
        results[case] = {'status': 'PASS'}
        try:
            yield
        except Exception as exc:
            results[case] = {'status': 'FAIL', 'error_type': type(exc).__name__,
                             'failure_layer': 'API' if isinstance(exc, httpx.HTTPError) else 'MODEL_CONTENT' if isinstance(exc, (ValidationError, AIOutputError, ValueError)) else 'DATA_FLOW'}

    await bot.start(workers=False)
    try:
        with check('T01'):
            forecast, _ = await bot.weather.refresh('F-D0047-089', '\u65b0\u5317\u5e02')
            assert forecast['periods']
            results['T01'].update(periods=len(forecast['periods']), forecast_hash=forecast['forecast_hash'])
        if results['T01']['status'] == 'FAIL':
            pytest.fail('Live CWA failed; inspect logs/live/latest.json', pytrace=False)
        await bot.db.ensure_user('Ulive-test')
        await bot.db.execute("UPDATE users SET city=?,dataset='F-D0047-089',location_code=?,setup_step='done' WHERE id='Ulive-test'",
                             ('\u65b0\u5317\u5e02', forecast['location_code']))
        user = await bot.db.one("SELECT * FROM users WHERE id='Ulive-test'")
        with check('T03'):
            await chat(bot, '\u670b\u53cb\u90fd\u53eb\u6211\u6d77\u9e7d\u5e03\u4e01\uff0c\u6211\u4e5f\u6bd4\u8f03\u559c\u6b61\u9019\u500b\u7a31\u547c\u3002', uid=user['id'])
            state = await bot.db.one('SELECT choices FROM conversation_state')
            selected = json.loads(state['choices'])[1]['user_text']
            await chat(bot, '2', uid=user['id'])
            assert model.calls[-1]['input']['user_text'] == selected
            await chat(bot, '\u660e\u5929\u9084\u6c92\u6c7a\u5b9a\u5e7e\u9ede\u51fa\u9580\u3002', uid=user['id'])
            assert len(model.calls) == 3
            for call in model.calls:
                Reply.model_validate(call['output']).for_mode('chat')
            assert max(r['elapsed_seconds'] for r in line.replies) < 60
            results['T03']['reply_seconds'] = [r['elapsed_seconds'] for r in line.replies]
        memory = None
        with check('T04'):
            await bot.db.execute('UPDATE messages SET created_at=created_at-121 WHERE user_id=?', (user['id'],))
            await bot.db.execute('UPDATE users SET morning_enabled=0')
            await bot.tick()
            assert await bot.db.one("SELECT id FROM jobs WHERE kind='memory' AND status='pending'")
            await bot.run_background()
            memory = await bot.db.one("SELECT * FROM memories WHERE key='persona.address_user_as'")
            assert memory and memory['value'] == '\u6d77\u9e7d\u5e03\u4e01'
            await bot.db.close()
            await bot.db.open()
            reply, context = await bot.chat.generate(user, '\u65e9\u5b89', fresh=True)
            assert context['recent'] == [] and context['summary'] == {}
            assert '\u6d77\u9e7d\u5e03\u4e01' in ''.join(reply.sentences)
            results['T04'].update(memory_id=memory['id'], sources=json.loads(memory['source_message_ids']), reply=reply.model_dump())
        with check('T02'):
            await bot.db.execute('UPDATE users SET morning_enabled=1')
            target = (clock.now() + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
            before = len(model.calls)
            await bot.prepare_morning(user['id'], target)
            row = await bot.db.one("SELECT * FROM outbox WHERE kind='morning_brief'")
            assert row and len(model.calls) == before + 1
            raw = Reply.model_validate(model.calls[-1]['output']).for_mode('morning_brief')
            assert json.loads(row['payload'])['messages'][0]['text'].startswith(raw.sentences[0])
            results['T02'].update(outbox_id=row['id'], memory_ids=json.loads(row['memory_ids']))
        with check('T05'):
            count = (await bot.db.one("SELECT COUNT(*) AS n FROM messages WHERE role='assistant' AND kind='chat'"))['n']
            for turn in range(count+1, 20):
                for role, content in (('user', 'daily conversation'), ('assistant', 'A short reply.')):
                    await bot.db.execute('INSERT INTO messages(user_id,role,content,turn,created_at) VALUES (?,?,?,?,?)',
                                         (user['id'], role, content, turn, clock.now().timestamp()))
            await chat(bot, '\u4eca\u5929\u804a\u5f97\u5f88\u958b\u5fc3', uid=user['id'])
            assert len(await bot.db.all("SELECT id FROM jobs WHERE kind='compact'")) == 1
            await bot.memory.organize(user['id'], compact=True)
            state = await bot.db.one('SELECT * FROM conversation_state')
            assert state['summary_through'] == 30
            summary = json.loads(state['summary'])
            assert '\u51fa\u9580' in json.dumps(summary, ensure_ascii=False)
            context = await bot.chat.context(user['id'])
            assert len(context['recent']) == 10
            assert len(await bot.db.all('SELECT id FROM messages')) == 40
            results['T05'].update(summary=summary, summary_through=30, recent_messages=10)
    finally:
        report['model_calls'] = model.calls
        report['call_count'] = len(model.calls)
        report['max_reply_seconds'] = max((r['elapsed_seconds'] for r in line.replies), default=None)
        report['background_memory_excluded'] = True
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        await bot.stop()
    if any(r['status'] != 'PASS' for r in results.values()):
        pytest.fail('Live acceptance has failed cases; inspect logs/live/latest.json', pytrace=False)
