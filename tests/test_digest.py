import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('BOT_TOKEN', 'test')
logging.getLogger('main').disabled = True

from peewee import SqliteDatabase
from requests import RequestException

from main import Article, build_digest_messages, send_daily_digest


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.database = SqliteDatabase(':memory:')
        self.binding = self.database.bind_ctx([Article])
        self.binding.__enter__()
        self.database.create_tables([Article])

    def tearDown(self):
        self.binding.__exit__(None, None, None)
        self.database.close()

    def test_groups_areas_in_digest_order(self):
        areas = [
            ('altro', 'Altro'),
            ('italia', 'Italia'),
            ('friuli_venezia_giulia', 'Friuli-Venezia Giulia'),
            ('lombardia', 'Lombardia'),
            ('veneto', 'Veneto'),
            ('tirolo', 'Tirolo'),
            ('lago_di_garda', 'Lago di Garda'),
        ]
        articles = [
            SimpleNamespace(id=index, area=area, title=f'Title {index} &', link=f'https://x/{index}')
            for index, (area, _) in enumerate(areas)
        ]

        [(text, article_ids)] = build_digest_messages(articles)

        expected_labels = [label for _, label in reversed(areas)]
        self.assertEqual(sorted(expected_labels, key=text.index), expected_labels)
        self.assertEqual(article_ids, list(reversed(range(len(areas)))))
        self.assertIn('&amp;', text)

    def test_splits_without_losing_articles(self):
        articles = [
            SimpleNamespace(id=index, area='veneto', title='x' * 2000, link=f'https://x/{index}')
            for index in range(3)
        ]

        messages = build_digest_messages(articles)

        self.assertGreater(len(messages), 1)
        self.assertEqual([article_id for _, ids in messages for article_id in ids], [0, 1, 2])
        self.assertTrue(all(len(text) <= 4096 for text, _ in messages))

    @patch('main.requests.post')
    def test_marks_only_digest_articles_after_send(self, post):
        digest = Article.create(
            post_id=1,
            title='Digest title',
            link='https://example.com/digest',
            published=1,
            area='veneto',
            is_digest=True,
        )
        immediate = Article.create(
            post_id=2,
            title='Immediate title',
            link='https://example.com/immediate',
            published=2,
            area='trento',
        )
        post.return_value = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {'result': {'message_id': 456}},
        )

        send_daily_digest()

        self.assertEqual(Article.get_by_id(digest.id).telegram_message_id, 456)
        self.assertIsNone(Article.get_by_id(immediate.id).telegram_message_id)

    @patch('main.requests.post', side_effect=RequestException)
    def test_failed_send_leaves_articles_pending(self, _post):
        article = Article.create(
            post_id=1,
            title='Title',
            link='https://example.com/article',
            published=1,
            area='veneto',
            is_digest=True,
        )

        send_daily_digest()

        self.assertIsNone(Article.get_by_id(article.id).telegram_message_id)


if __name__ == '__main__':
    unittest.main()
