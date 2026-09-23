import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('BOT_TOKEN', 'test')
logging.getLogger('main').disabled = True

from main import classify_article


class ClassifierTest(unittest.TestCase):
    @patch('main.requests.post')
    def test_returns_area_and_audit_data(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            'model': 'served-model',
            'choices': [{'message': {
                'reasoning_details': [
                    {'type': 'reasoning.summary', 'summary': 'Summary'},
                    {'type': 'reasoning.text', 'text': 'Raw reasoning'},
                ],
                'content': '{"area":"tirolo","place":"Halltal"}',
            }}],
        }

        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test'}):
            result = classify_article('TITLE_SENTINEL', 'DESCRIPTION_SENTINEL', 'EXCERPT_SENTINEL')

        self.assertEqual(result, ('tirolo', 'Halltal', 'Raw reasoning', 'served-model'))
        prompt = post.call_args.kwargs['json']['messages'][0]['content']
        for sentinel in ('TITLE_SENTINEL', 'DESCRIPTION_SENTINEL', 'EXCERPT_SENTINEL'):
            self.assertIn(sentinel, prompt)

    @patch('main.requests.post')
    def test_invalid_response_falls_back_to_altro(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'choices': [{'message': {'content': None}}]}

        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test'}):
            self.assertEqual(
                classify_article('title', 'description', 'excerpt'),
                ('altro', None, None, None),
            )

    @patch('main.requests.post')
    def test_truncated_response_falls_back_to_altro(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            'model': 'served-model',
            'choices': [{
                'finish_reason': 'length',
                'message': {'reasoning': 'Too much thinking', 'content': None},
            }],
        }

        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test'}):
            result = classify_article('title', 'description', 'excerpt')

        self.assertEqual(result, ('altro', None, 'Too much thinking', 'served-model'))

    @patch('main.time.sleep')
    @patch('main.requests.post')
    def test_retries_rate_limit(self, post, sleep):
        post.side_effect = [
            SimpleNamespace(status_code=429, text='rate limited'),
            SimpleNamespace(
                status_code=200,
                json=lambda: {'choices': [{'message': {
                    'content': '{"area":"italia","place":null}'
                }}]},
            ),
        ]

        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test'}):
            result = classify_article('title', 'description', 'excerpt')

        self.assertEqual(result, ('italia', None, None, None))
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch('main.time.sleep')
    @patch('main.requests.post')
    def test_retries_server_errors(self, post, sleep):
        post.side_effect = [
            SimpleNamespace(status_code=500, text='server error'),
            SimpleNamespace(status_code=599, text='server error'),
            SimpleNamespace(
                status_code=200,
                json=lambda: {'choices': [{'message': {
                    'content': '{"area":"italia","place":null}'
                }}]},
            ),
        ]

        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test'}):
            result = classify_article('title', 'description', 'excerpt')

        self.assertEqual(result, ('italia', None, None, None))
        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])


if __name__ == '__main__':
    unittest.main()
