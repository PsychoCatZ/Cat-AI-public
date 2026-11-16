import tempfile
import unittest
from pathlib import Path
from catai.app import Config, Secretary, State, ServiceError, INTRO, Telegram


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.rights = {'can_reply': True}
        self.enabled = True
        self.owner = 1

    def call(self, method, **args):
        if method == 'getBusinessConnection':
            return {'id': 'bc', 'user': {'id': self.owner},
                    'is_enabled': self.enabled, 'rights': self.rights}
        if method == 'sendMessage':
            self.sent.append(args)
            return {'message_id': 100}
        raise AssertionError(method)


class FakeModel:
    def __init__(self):
        self.calls = []
        self.hook = lambda: None

    def reply(self, history):
        self.calls.append(history.copy())
        self.hook()
        return 'Я CatAI. Давай поболтаем!'


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(str(Path(self.temp.name) / 'state.sqlite3'))
        self.tg, self.model = FakeTelegram(), FakeModel()
        self.clock = 100000
        self.c = Config('fake', 1, 2, True, 'https://model.invalid/v1/chat/completions',
                        'fake', 'test', 'Test model', 'https://model.invalid/privacy', '')
        self.app = Secretary(self.c, self.state, self.tg, self.model, lambda: self.clock)
        self.uid = 0

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def command(self, text, sender=1):
        self.uid += 1
        self.app.handle({'update_id': self.uid, 'message': {'message_id': self.uid,
            'date': self.clock, 'from': {'id': sender},
            'chat': {'id': sender, 'type': 'private'}, 'text': text}})

    def message(self, text='Привет', **overrides):
        self.uid += 1
        m = {'message_id': self.uid, 'date': self.clock, 'business_connection_id': 'bc',
             'from': {'id': 2}, 'chat': {'id': 2, 'type': 'private'}, 'text': text}
        m.update(overrides)
        u = {'update_id': self.uid, 'business_message': m}
        self.app.handle(u)
        return u

    def consent(self):
        self.command('/sleep')
        self.message()
        self.message('Давай поболтаем')
        self.clock += 3

    def test_awake_and_owner_only(self):
        self.message()
        self.command('/sleep', sender=2)
        self.message()
        self.assertFalse(self.app.sleeping)
        self.assertEqual(self.tg.sent, [])

    def test_intro_consent_context_and_business_send(self):
        self.consent()
        self.assertTrue(self.tg.sent[1]['text'].startswith(INTRO))
        self.assertEqual(self.model.calls, [])
        self.message('Люблю котов')
        self.clock += 3
        self.message('Что я люблю?')
        self.assertEqual(self.model.calls[-1][0]['content'], 'Люблю котов')
        self.assertEqual(self.tg.sent[-1]['business_connection_id'], 'bc')
        self.assertEqual(self.tg.sent[-1]['chat_id'], 2)

    def test_no_text_sent_before_consent(self):
        self.command('/sleep')
        self.message('Секрет')
        self.message('Ещё текст')
        self.assertFalse(self.model.calls)

    def test_dedup_update_and_message(self):
        self.consent()
        u = self.message('Тест')
        count = len(self.tg.sent)
        self.app.handle(u)
        u['update_id'] += 100
        self.app.handle(u)
        self.assertEqual(len(self.tg.sent), count)
        self.assertEqual(len(self.model.calls), 1)

    def test_disabled_rights_foreign_owner_and_excess_rights(self):
        self.command('/sleep')
        for rights, enabled, owner in [({}, True, 1), ({'can_reply': True}, False, 1),
                ({'can_reply': True}, True, 9),
                ({'can_reply': True, 'can_delete_all_messages': True}, True, 1),
                ({'can_reply': True, 'can_edit_name': True}, True, 1)]:
            self.tg.rights, self.tg.enabled, self.tg.owner = rights, enabled, owner
            self.message()
        self.assertEqual(len(self.tg.sent), 1)  # owner command only

    def test_wrong_chat_bot_outgoing_old_and_group(self):
        self.command('/sleep')
        for fields in [{'chat': {'id': 3, 'type': 'private'}},
                       {'from': {'id': 1}}, {'from': {'id': 2, 'is_bot': True}},
                       {'sender_business_bot': {'id': 7}},
                       {'date': self.clock - 86400},
                       {'chat': {'id': 2, 'type': 'group'}}]:
            self.message(**fields)
        self.assertEqual(len(self.tg.sent), 1)

    def test_recheck_revocation_after_model(self):
        self.consent()
        self.model.hook = lambda: setattr(self.tg, 'enabled', False)
        count = len(self.tg.sent)
        self.message()
        self.assertEqual(len(self.tg.sent), count)

    def test_recheck_window_after_model(self):
        self.consent()
        self.model.hook = lambda: setattr(self, 'clock', self.clock + 86400)
        count = len(self.tg.sent)
        self.message()
        self.assertEqual(len(self.tg.sent), count)

    def test_awake_clears_context(self):
        self.consent()
        self.message()
        self.command('/awake')
        count = len(self.model.calls)
        self.message()
        self.assertFalse(self.app.history)
        self.assertEqual(len(self.model.calls), count)

    def test_stop_persists_and_ttl_clears(self):
        self.consent()
        self.message('/stop')
        self.message()
        self.assertEqual(self.state.get('blocked:2'), '1')
        self.assertFalse(self.app.history)

    def test_ttl_and_restart(self):
        self.consent()
        self.clock += 3601
        self.app.prune()
        self.assertFalse(self.app.history)
        self.assertTrue(self.app.has_consent(2))
        restarted = Secretary(self.c, self.state, self.tg, self.model, lambda: self.clock)
        self.assertFalse(restarted.sleeping)

    def test_consent_survives_restart_awake_and_expiry(self):
        self.consent()
        self.message('Кот')
        self.command('/awake')
        self.assertTrue(self.app.has_consent(2))
        self.app = Secretary(self.c, self.state, self.tg, self.model, lambda: self.clock)
        self.command('/sleep')
        self.clock += 3
        self.message('Продолжим')
        self.assertEqual(self.model.calls[-1][-1]['content'], 'Продолжим')
        self.clock += 3601
        self.app.prune()
        self.message('После паузы')
        self.assertEqual(self.model.calls[-1][-1]['content'], 'После паузы')

    def test_forget_preserves_consent_and_removes_history(self):
        self.consent()
        self.message('Личная деталь')
        self.message('Очистить контекст')
        self.assertTrue(self.app.has_consent(2))
        self.assertFalse(self.app.history)
        self.clock += 3
        self.message('Новая тема')
        self.assertEqual(self.model.calls[-1], [{'role': 'user', 'content': 'Новая тема'}])

    def test_revoke_persists_and_can_be_granted_again(self):
        self.consent()
        self.message('Хватит')
        self.assertFalse(self.app.has_consent(2))
        count = len(self.model.calls)
        self.app = Secretary(self.c, self.state, self.tg, self.model, lambda: self.clock)
        self.command('/sleep')
        self.message('Не отвечай')
        self.assertEqual(len(self.model.calls), count)
        self.message('Давай поболтаем')
        self.assertTrue(self.app.has_consent(2))
        self.clock += 3
        self.message('Снова привет')
        self.assertEqual(len(self.model.calls), count + 1)

    def test_consent_requires_notice_and_matches_terms(self):
        self.command('/sleep')
        self.message('Давай поболтаем')
        self.assertFalse(self.app.has_consent(2))
        self.message('Давай поболтаем')
        self.assertTrue(self.app.has_consent(2))
        from dataclasses import replace
        self.app.c = replace(self.c, provider='Changed provider')
        self.assertFalse(self.app.has_consent(2))

    def test_telegram_scope_multiple_chats_isolated(self):
        from dataclasses import replace
        self.app.c = replace(self.c, chat_scope='telegram', test_chat=0)
        self.consent()
        self.message('Первый диалог')
        def other(text):
            return self.message(text, chat={'id': 3, 'type': 'private'}, **{'from': {'id': 3}})
        other('Привет из другого чата')
        count = len(self.model.calls)
        other('Без согласия')
        self.assertEqual(len(self.model.calls), count)
        other('Давай поболтаем')
        self.clock += 3
        other('Второй диалог')
        self.assertEqual(self.model.calls[-1], [{'role': 'user', 'content': 'Второй диалог'}])
        self.assertTrue(self.app.has_consent(2))
        self.assertTrue(self.app.has_consent(3))
        other('Хватит')
        self.assertTrue(self.app.has_consent(2))
        self.assertFalse(self.app.has_consent(3))

    def test_edit_event_clears_context_without_error(self):
        self.consent()
        self.message('Текст')
        self.uid += 1
        self.app.handle({'update_id': self.uid, 'edited_business_message': {}})
        self.assertFalse(self.app.history)
        self.assertTrue(self.app.has_consent(2))

    def test_durable_claim(self):
        self.assertTrue(self.state.claim('example'))
        other = State(str(Path(self.temp.name) / 'state.sqlite3'))
        try:
            self.assertFalse(other.claim('example'))
        finally:
            other.db.close()

    def test_forbidden_methods(self):
        for method in ['deleteMessages', 'setBusinessAccountName', 'transferBusinessAccountStars']:
            with self.assertRaises(ServiceError):
                Telegram('fake').call(method)


if __name__ == '__main__':
    unittest.main()
