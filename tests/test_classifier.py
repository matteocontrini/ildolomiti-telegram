import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('BOT_TOKEN', 'test')
logging.getLogger('main').disabled = True

from main import classify_area


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
                'content': '{"area":"tirolo"}',
            }}],
        }

        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test'}):
            result = classify_area('TITLE_SENTINEL', 'DESCRIPTION_SENTINEL', 'EXCERPT_SENTINEL')

        self.assertEqual(result, ('tirolo', 'Raw reasoning', 'served-model'))
        prompt = post.call_args.kwargs['json']['messages'][0]['content']
        for sentinel in ('TITLE_SENTINEL', 'DESCRIPTION_SENTINEL', 'EXCERPT_SENTINEL'):
            self.assertIn(sentinel, prompt)

    @patch('main.requests.post')
    def test_invalid_response_falls_back_to_altro(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'choices': [{'message': {'content': None}}]}

        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test'}):
            self.assertEqual(classify_area('title', 'description', 'excerpt'), ('altro', None, None))

    @patch('main.time.sleep')
    @patch('main.requests.post')
    def test_retries_rate_limit(self, post, sleep):
        post.side_effect = [
            SimpleNamespace(status_code=429, text='rate limited'),
            SimpleNamespace(
                status_code=200,
                json=lambda: {'choices': [{'message': {'content': '{"area":"italia"}'}}]},
            ),
        ]

        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test'}):
            result = classify_area('title', 'description', 'excerpt')

        self.assertEqual(result, ('italia', None, None))
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)


if __name__ == '__main__':
    unittest.main()
