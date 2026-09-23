import logging
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import call, mock_open, patch

from requests import ConnectionError

os.environ.setdefault('BOT_TOKEN', 'test')
logging.getLogger('main').disabled = True

from peewee import SqliteDatabase

from main import Article, download_image, fetch_article_details, process_new_article


def entry(post_id: int):
    return SimpleNamespace(
        title=f'Article {post_id}',
        link=f'https://www.ildolomiti.it/cronaca/2026/article-{post_id}',
        description='Feed description',
        published_parsed=time.gmtime(post_id),
    )


class ArticleTest(unittest.TestCase):
    def setUp(self):
        self.database = SqliteDatabase(':memory:')
        self.binding = self.database.bind_ctx([Article])
        self.binding.__enter__()
        self.database.create_tables([Article])

    def tearDown(self):
        self.binding.__exit__(None, None, None)
        self.database.close()

    @patch('main.requests.get')
    def test_extracts_two_nonempty_paragraphs_and_area(self, get):
        get.return_value = SimpleNamespace(
            text='''
                <a class="zona-marker" href="/belluno">Belluno</a>
                <article id="node-123">
                    <div class="artSub">Description</div>
                    <div class="artImage"><img src="/sites/default/files/lazy.jpg"
                        data-src="https://cdn.ildolomiti.it/opening.jpg"></div>
                    <div class="field-name-body">
                        <p>First paragraph.</p>
                        <p>&nbsp;</p>
                        <p>Second paragraph.</p>
                        <p>Third paragraph.</p>
                    </div>
                </article>
                <meta property="og:image" content="https://www.ildolomiti.it/og.jpg">
                <link rel="image_src" href="https://cdn.ildolomiti.it/opening.jpg">
                <meta name="twitter:image" content="https://cdn.ildolomiti.it/original.jpg">
            ''',
            raise_for_status=lambda: None,
        )

        details = fetch_article_details('https://example.com/article')

        self.assertEqual(details['excerpt'], 'First paragraph. Second paragraph.')
        self.assertEqual(details['image_urls'], [
            'https://cdn.ildolomiti.it/opening.jpg',
            'https://www.ildolomiti.it/og.jpg',
        ])
        self.assertEqual(details['marker'], 'belluno')
        self.assertEqual(details['area'], 'veneto')

    @patch('main.requests.Session')
    def test_tries_next_image_after_download_failure(self, session):
        session.return_value.get.side_effect = [
            ConnectionError('opening image unavailable'),
            SimpleNamespace(content=b'image', raise_for_status=lambda: None),
        ]
        with patch('builtins.open', mock_open()) as image_file:
            filename = download_image(['https://cdn.ildolomiti.it/opening.jpg',
                                       'https://www.ildolomiti.it/og.jpg'])

        self.assertTrue(filename.startswith('images/'))
        session.return_value.get.assert_has_calls([
            call('https://cdn.ildolomiti.it/opening.jpg', timeout=10),
            call('https://www.ildolomiti.it/og.jpg', timeout=10),
        ])
        image_file.assert_called_once_with(filename, 'wb')

    def test_routes_declared_areas(self):
        details = [
            {
                'post_id': 1,
                'description': 'Description',
                'excerpt': '',
                'image_urls': [],
                'marker': 'trento',
                'area': 'trento',
            },
            {
                'post_id': 2,
                'description': 'Description',
                'excerpt': '',
                'image_urls': [],
                'marker': 'belluno',
                'area': 'veneto',
            },
        ]
        with (
            patch('main.fetch_article_details', side_effect=details),
            patch('main.classify_article', return_value=(
                'veneto', 'Trento', 'Reasoning', 'served-model'
            )) as classify,
            patch('main.send_classification_log'),
            patch('main.send_message', return_value=42) as send,
        ):
            process_new_article(entry(1))
            process_new_article(entry(2))

        immediate = Article.get(post_id=1)
        digest = Article.get(post_id=2)
        self.assertFalse(immediate.is_digest)
        self.assertEqual(immediate.telegram_message_id, 42)
        self.assertTrue(digest.is_digest)
        self.assertIsNone(digest.telegram_message_id)
        classify.assert_called_once()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0].place, 'Trento')

    def test_queues_classification_with_audit_data(self):
        with (
            patch('main.fetch_article_details', return_value={
                'post_id': 123,
                'description': 'Description',
                'excerpt': 'Excerpt',
                'image_urls': [],
                'marker': None,
                'area': None,
            }),
            patch('main.classify_article', return_value=(
                'veneto', 'Belluno', 'Located near Belluno.', 'served-model'
            )),
            patch('main.send_classification_log') as classification_log,
            patch('main.send_message') as send,
        ):
            process_new_article(entry(123))

        article = Article.get(post_id=123)
        self.assertTrue(article.is_digest)
        self.assertEqual(article.area, 'veneto')
        self.assertEqual(article.classification_reasoning, 'Located near Belluno.')
        self.assertEqual(article.classification_model, 'served-model')
        classification_log.assert_called_once_with(
            'Article 123', entry(123).link, None, 'veneto', 'Belluno',
            'Located near Belluno.', 'served-model'
        )
        send.assert_not_called()

    def test_updates_digest_article_by_post_id(self):
        Article.create(
            post_id=123,
            title='Old title',
            link='https://www.ildolomiti.it/cronaca/2026/old-title',
            published=1,
            area='veneto',
            is_digest=True,
        )
        changed = entry(123)
        changed.title = 'New title'
        changed.link = 'https://www.ildolomiti.it/cronaca/2026/new-title'

        with patch('main.fetch_article_details', return_value={
            'post_id': 123,
            'description': 'Description',
            'excerpt': '',
            'image_urls': [],
            'marker': 'belluno',
            'area': 'veneto',
        }):
            process_new_article(changed)

        article = Article.get(post_id=123)
        self.assertEqual((article.title, article.link), (changed.title, changed.link))
        self.assertEqual(Article.select().count(), 1)


if __name__ == '__main__':
    unittest.main()
