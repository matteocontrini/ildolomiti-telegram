import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from difflib import ndiff
from hashlib import md5
from typing import Optional

import feedparser
import humanize
import requests
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup
from peewee import SqliteDatabase, Model, TextField, IntegerField, BooleanField
from requests import RequestException
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

BOT_TOKEN = os.environ['BOT_TOKEN']
TELEGRAM_API_URL = f'https://api.telegram.org/bot{BOT_TOKEN}'
TELEGRAM_CHANNEL = os.environ.get('TELEGRAM_CHANNEL', '@ildolomitinews')
TELEGRAM_LOGS_CHANNEL = -1001626800013

DATABASE_PATH = os.environ.get('DATABASE_PATH', 'ildolomiti.db')
OPENROUTER_MODELS = os.environ.get(
    'OPENROUTER_MODELS',
    'openai/gpt-oss-120b,qwen/qwen3.8-flash,mistralai/mistral-small-2603',
).split(',')

USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/152.0.0.0 Safari/537.36'
)

# Some articles are explicitly marked with an area, we map them to these areas
MARKERS_TO_AREAS = {
    'trento': 'trento',
    'bolzano': 'bolzano',
    'belluno': 'veneto',
    'sondrio': 'lombardia',
    'fvg': 'friuli_venezia_giulia',
}

# Articles without an explicit area are classified by AI to one of these areas
CLASSIFIED_AREAS = {
    'trento',
    'bolzano',
    'veneto',
    'lombardia',
    'friuli_venezia_giulia',
    'lago_di_garda',
    'tirolo',
    'italia',
    'altro',
}

# Only these two areas are immediately published, the others go in the daily digest
IMMEDIATE_PUBLISHING_AREAS = {'trento', 'bolzano'}

# Digest rendering
DIGEST_AREAS = (
    ('lago_di_garda', 'Lago di Garda'),
    ('tirolo', 'Tirolo'),
    ('veneto', 'Veneto'),
    ('lombardia', 'Lombardia'),
    ('friuli_venezia_giulia', 'Friuli-Venezia Giulia'),
    ('italia', 'Italia'),
    ('altro', 'Altro'),
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%dT%H:%M:%S%z', stream=sys.stdout)
logger = logging.getLogger(__name__)

logger.info('Database path: ' + DATABASE_PATH)

db = SqliteDatabase(DATABASE_PATH)


class Article(Model):
    post_id = IntegerField(null=True, unique=True)
    title = TextField()
    link = TextField(unique=True)
    published = IntegerField()
    telegram_message_id = IntegerField(null=True)
    is_digest = BooleanField(default=False)
    area = TextField(null=True)
    classification_reasoning = TextField(null=True)
    classification_model = TextField(null=True)

    class Meta:
        database = db


@dataclass
class TelegramMessage:
    title: str
    link: str
    tags: list[str]
    description: str
    image: str
    place: Optional[str] = None


def check():
    logger.info('Checking...')

    url = 'https://www.ildolomiti.it/rss.xml?_=' + str(int(time.time()))
    logger.info(f'Fetching {url}')

    resp = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=10)

    if resp.status_code != 200:
        logger.error(f'Error fetching feed ({resp.status_code}): {resp.text}')
        return

    feed = feedparser.parse(resp.text)

    if feed.bozo:
        logger.error(f'Error parsing feed: {feed.bozo_exception}')
        return

    if Article.select().count() == 0:
        first_run(feed)
        return

    for entry in reversed(feed.entries):
        if not Article.get_or_none(link=entry.link):
            process_new_article(entry)

    logger.info('Done!')


def first_run(feed):
    logger.info('First run, populating database...')
    for entry in reversed(feed.entries):
        article = Article(
            post_id=None,
            title=entry.title,
            link=entry.link,
            published=int(time.mktime(entry.published_parsed)),
            telegram_message_id=None
        )
        article.save()
    logger.info('Done!')


def process_new_article(entry):
    tag = re.match(r'https://www\.ildolomiti\.it/([a-z-]+)/', entry.link)
    tag = tag.group(1) if tag else None
    if tag == 'blog' or tag == 'necrologi' or tag == 'video':
        return

    # es. "ricerca-e-universita" -> #ricerca #universita
    if tag and '-' in tag:
        tags = tag.split('-')
        tags = [t for t in tags if len(t) > 1]
    else:
        tags = [tag] if tag else []

    details = fetch_article_details(entry.link)
    description = details['description'] or entry.description

    post_id = details['post_id']
    article = Article.get_or_none(post_id=post_id) if post_id else None

    # Post was already queued for the digest, but the link/title have changed
    if article and article.is_digest:
        logger.info(f'Updating digest article: {entry.link} (old: {article.link})')
        article.title = entry.title.strip()
        article.link = entry.link
        article.save()
        return

    place = None
    classification_reasoning = None
    classification_model = None
    area = details['area']

    # LLM call, used for two purposes:
    # - Extract the place for articles published immediately.
    # - Extract also the area when it's not declared, so we can decide whether to publish it immediately.
    if not area or area in IMMEDIATE_PUBLISHING_AREAS:
        if area:
            logger.info(f'Extracting place for {entry.link}...')
        else:
            logger.info(f'Area not declared for {entry.link}, classifying + extracting place...')
        classified_area, place, classification_reasoning, classification_model = classify_article(
            entry.title, description, details['excerpt']
        )
        if not area:
            area = classified_area
            logger.info(f'Classified {entry.link} as {area}')
        if place:
            logger.info(f'Extracted place "{place}" for {entry.link}')
        else:
            logger.info(f'No place extracted for {entry.link}')
        send_classification_log(
            entry.title, entry.link, details['marker'], area, place,
            classification_reasoning, classification_model
        )

    # Don't publish if the declared or classified area is not for immediate publishing
    if not article and area not in IMMEDIATE_PUBLISHING_AREAS:
        logger.info(f'Queuing {area} article: {entry.link}')
        Article.create(
            post_id=post_id,
            title=entry.title.strip(),
            link=entry.link,
            published=int(time.mktime(entry.published_parsed)),
            area=area,
            is_digest=True,
            classification_reasoning=classification_reasoning,
            classification_model=classification_model,
        )
        return

    message = TelegramMessage(
        title=entry.title.strip(),
        link=entry.link,
        tags=tags,
        description=description,
        image=download_image(details['image_url']) or 'fallback.jpg',
        place=place,
    )

    # The post could be matched by post ID but not by link (and therefore title), which means
    # the title changed and we should update the Telegram message.
    if article:
        article: Article
        logger.info(f'Updating article: {entry.link} (old: {article.link})')
        if not article.telegram_message_id:
            logger.error('Article has no telegram_message_id, skipping')
            return
        try:
            send_message(message, article.telegram_message_id)
        except RequestException:
            logger.exception('Error updating message')
            return  # so that it's retried later
        send_title_diff_log(article, entry)
        article.title = message.title
        article.link = message.link
        article.save()
    # Otherwise assume that it's new
    else:
        logger.info(f'Sending article: {entry.link}')
        try:
            message_id = send_message(message)
        except RequestException:
            logger.exception('Error sending message')
            return  # so that it's retried later
        Article.create(
            post_id=details['post_id'],
            title=message.title,
            link=message.link,
            published=time.mktime(entry.published_parsed),
            telegram_message_id=message_id,
            area=area,
            classification_reasoning=classification_reasoning,
            classification_model=classification_model,
        )


def fetch_article_details(link: str) -> dict:
    resp = requests.get(
        link + '?_=' + str(int(time.time())),  # fix for 404 ending up in the dolomiti cache
        headers={'User-Agent': USER_AGENT},
        timeout=10
    )

    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, 'html.parser')

    post_id = None
    description = None
    excerpt = ''
    image_url = None

    article = soup.select_one('article[id^="node-"]')
    if article:
        post_id = article['id'].split('-')[1]
        description = article.find('div', class_='artSub')
        if description:
            description = description.text.strip()
        else:
            logger.error('Description not found')
        # Extract first two non-empty paragraphs capped to 2000 chars
        paragraphs = [
            text for paragraph in article.select('.field-name-body p')
            if (text := paragraph.get_text(' ', strip=True))
        ][:2]
        excerpt = ' '.join(paragraphs)[:2000]
        image_url = soup.find('meta', property='og:image')
        if image_url:
            image_url = image_url['content']
        else:
            logger.error('Image meta tag not found')
    else:
        logger.error('Article node not found')

    # Extract the area marker, e.g. "/sondrio" -> "sondrio"
    marker = soup.select_one('.zona-marker[href]')
    marker = marker['href'].strip('/') if marker else None
    # And map it to the area name, e.g. "sondrio" -> "lombardia"
    area = MARKERS_TO_AREAS.get(marker) if marker else None

    return {
        'post_id': post_id,
        'description': description,
        'excerpt': excerpt,
        'image_url': image_url,
        'marker': marker,
        'area': area,
    }


def classify_article(title: str, description: str, excerpt: str) \
        -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    api_key = os.environ.get('OPENROUTER_API_KEY')
    if not api_key:
        logger.error('OPENROUTER_API_KEY is not set, classifying article as altro')
        return 'altro', None, None, None

    classification_reasoning = None
    served_model = None

    prompt = f'''Classify this news article published by Il Dolomiti by its primary geographic area and extract the place where the event happened.

Choose exactly one area:
- trento: Province of Trento
- bolzano: Province of Bolzano
- veneto: Veneto, except articles primarily about Lake Garda
- lombardia: Lombardia, except articles primarily about Lake Garda
- friuli_venezia_giulia: Friuli-Venezia Giulia
- lago_di_garda: primarily about Lake Garda or a place directly on its shores, only if outside the Province of Trento
- tirolo: Austrian state of Tyrol, excluding Alto Adige/Südtirol
- italia: elsewhere in Italy or a national Italian story
- altro: outside Italy or the location cannot be determined

For place, first determine whether the event is an accident or a mishap.
- If so, extract the name of the place (city, locality, mountain, etc.) where the event happened.
  Multiple names are allowed.
  Include road name (e.g. A22, SS47) when available.
  Use the broad area if the place cannot be determined.
  Do not infer names from memory.
- If not, return null.

Output example:
{{ "area": "trento", "place": "Dro" }}

<title>{title}</title>
<description>{description}</description>
<excerpt>{excerpt}</article>
'''

    try:
        payload = {
            'models': OPENROUTER_MODELS,
            'messages': [{'role': 'user', 'content': prompt}],
            'reasoning': {'effort': 'medium'},
            'max_tokens': 2048,
            'provider': {
                'require_parameters': True,
                'sort': {'by': 'price', 'partition': 'model'},
                'preferred_min_throughput': {'p50': 100},
            },
            'response_format': {
                'type': 'json_schema',
                'json_schema': {
                    'name': 'area_classification',
                    'strict': True,
                    'schema': {
                        'type': 'object',
                        'properties': {
                            'area': {'type': 'string', 'enum': sorted(CLASSIFIED_AREAS)},
                            'place': {'type': ['string', 'null']},
                        },
                        'required': ['area', 'place'],
                        'additionalProperties': False,
                    },
                },
            }
        }
        for attempt in range(3):
            response = requests.post(
                'https://openrouter.ai/api/v1/chat/completions',
                headers={'Authorization': f'Bearer {api_key}'},
                json=payload,
                timeout=60,
            )
            if response.status_code != 429 or attempt == 2:
                break
            wait = 2 ** attempt
            logger.warning(f'OpenRouter rate limited the request, retrying in {wait}s')
            time.sleep(wait)

        if response.status_code != 200:
            logger.error(
                f'OpenRouter API error ({response.status_code}): {response.text}. '
                'Classifying article as altro'
            )
            return 'altro', None, None, None

        response_data = response.json()
        response_model = response_data.get('model')
        served_model = response_model if isinstance(response_model, str) else None
        logger.info(f'OpenRouter used {served_model or "unknown model"}')
        choice = response_data['choices'][0]
        message = choice['message']
        assert isinstance(message, dict), 'OpenRouter returned an invalid message'

        # Providers normally expose their plaintext reasoning in this normalized field.
        reasoning = message.get('reasoning')
        classification_reasoning = reasoning if isinstance(reasoning, str) and reasoning else None
        if classification_reasoning is None and isinstance(message.get('reasoning_details'), list):
            # Otherwise inspect the structured blocks: prefer raw text, then summaries.
            details = message['reasoning_details']
            raw_reasoning = [detail['text'] for detail in details
                             if isinstance(detail, dict) and isinstance(detail.get('text'), str)]
            summaries = [detail['summary'] for detail in details
                         if isinstance(detail, dict) and isinstance(detail.get('summary'), str)]
            classification_reasoning = '\n'.join(raw_reasoning or summaries)
        if classification_reasoning:
            logger.info(f'{served_model or "unknown model"} thinking: {classification_reasoning}')

        if choice.get('finish_reason') == 'length':
            logger.error(
                f'{served_model or "unknown model"} reached max_tokens; discarding incomplete output'
            )
            return 'altro', None, classification_reasoning, served_model

        output_text = message.get('content')
        assert isinstance(output_text, str), 'OpenRouter returned invalid message content'
        result = json.loads(output_text)
        area = result['area']
        place = result.get('place')
        place = place.strip() if isinstance(place, str) and place.strip() else None
        return (
            area if area in CLASSIFIED_AREAS else 'altro',
            place,
            classification_reasoning,
            served_model,
        )
    except Exception:
        logger.exception('Error classifying article, classifying as altro')
        return 'altro', None, classification_reasoning, served_model


def download_image(image_url: str) -> Optional[str]:
    if not image_url:
        return None
    try:
        session = requests.Session()
        retries = Retry(total=2, status_forcelist=[502, 503, 504])
        session.mount('https://', HTTPAdapter(max_retries=retries))
        resp = session.get(image_url, timeout=10)
        resp.raise_for_status()
        filename = 'images/' + md5(image_url.encode('utf-8')).hexdigest()
        with open(filename, 'wb') as f:
            f.write(resp.content)
        return filename
    except (Exception,):
        logger.exception('Error downloading image')
        return None


def send_message(message: TelegramMessage, telegram_message_id=None) -> int:
    msg = ''
    if message.tags:
        for tag in message.tags:
            msg += f'#{tag} '
        msg += '— '

    msg += f'<strong>{telegram_escape(message.title)}</strong>'

    if message.description:
        msg += f'\n\n<i>{telegram_escape(message.description)}</i>'

    if message.place:
        msg += f'\n\n📍 {telegram_escape(message.place)}'

    msg += f'\n\n📰 <a href="{message.link}">Leggi articolo</a>'

    if telegram_message_id:
        payload = {
            'chat_id': TELEGRAM_CHANNEL,
            'message_id': telegram_message_id,
            'caption': msg,
            'parse_mode': 'HTML',
        }
        resp = requests.post(f'{TELEGRAM_API_URL}/editMessageCaption', json=payload, timeout=10)
    else:
        payload = {
            'chat_id': TELEGRAM_CHANNEL,
            'caption': msg,
            'parse_mode': 'HTML',
        }
        resp = requests.post(f'{TELEGRAM_API_URL}/sendPhoto',
                             data=payload,
                             files={
                                 'photo': open(message.image, 'rb')
                             }, timeout=10)

    # Error while editing
    if resp.status_code != 200 and telegram_message_id:
        # Log but don't raise (ignore error)
        logger.error(f'Error editing message: {resp.text}')
        return telegram_message_id
    # Error while sending
    elif resp.status_code != 200:
        logger.error(f'Error sending message: {resp.text}')
        resp.raise_for_status()

    return resp.json()['result']['message_id']


def send_title_diff_log(article: Article, entry):
    try:
        diff = get_diff(
            telegram_escape(article.title),
            telegram_escape(entry.title)
        )

        timeago = humanize.naturaltime(time.time() - article.published)

        requests.post(f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage', json={
            'chat_id': TELEGRAM_LOGS_CHANNEL,
            'text': f'{diff[0]}\n\n'
                    f'{diff[1]}\n\n'
                    f'<code>{telegram_escape(article.link)}</code>\n\n'
                    f'<code>{telegram_escape(entry.link)}</code>\n\n'
                    f'Message ID: <code>{article.telegram_message_id}</code>\n'
                    f'Published: {timeago}',
            'parse_mode': 'HTML',
        }, timeout=10)
    except (Exception,):
        logger.exception('Error sending log')


def send_classification_log(title: str, link: str, marker: Optional[str], area: str,
                            place: Optional[str], reasoning: Optional[str], model: Optional[str]):
    try:
        response = requests.post(f'{TELEGRAM_API_URL}/sendMessage', json={
            'chat_id': TELEGRAM_LOGS_CHANNEL,
            'text': f'<strong>{telegram_escape(title)}</strong>\n\n'
                    f'{telegram_escape(link)}\n\n'
                    f'Marker: <code>{telegram_escape(marker or "unknown")}</code>\n'
                    f'Area: <code>{telegram_escape(area)}</code>\n'
                    f'Place: <code>{telegram_escape(place or "unknown")}</code>\n'
                    f'Model: <code>{telegram_escape(model or "unknown")}</code>\n\n'
                    f'<pre>{telegram_escape(reasoning or "No reasoning returned")[:2500]}</pre>',
            'parse_mode': 'HTML',
        }, timeout=10)
        response.raise_for_status()
    except (Exception,):
        logger.exception('Error sending classification log')


def get_diff(old: str, new: str) -> list[str]:
    removed_from_old = get_diff_removals(old, new)
    removed_from_new = get_diff_removals(new, old)

    offset = 0
    for group in removed_from_old:
        start = group[0] + offset
        end = group[-1] + 1 + offset
        old = old[:start] + '<b><u>' + old[start:end] + '</u></b>' + old[end:]
        offset += len('<u></u><b></b>')

    offset = 0
    for group in removed_from_new:
        start = group[0] + offset
        end = group[-1] + 1 + offset
        new = new[:start] + '<b><u>' + new[start:end] + '</u></b>' + new[end:]
        offset += len('<u></u><b></b>')

    return [old, new]


def get_diff_removals(first: str, second: str) -> list:
    diff = ndiff(first, second)

    removed = []
    offset = 0

    for i, s in enumerate(diff):
        if s[0] == ' ':
            continue
        elif s[0] == '+':
            offset -= 1
        elif s[0] == '-':
            removed.append(i + offset)

    groups = []
    group = []
    for i in range(len(removed)):
        if i == 0:
            group.append(removed[i])
        elif removed[i] - removed[i - 1] == 1:
            group.append(removed[i])
        else:
            groups.append(group)
            group = [removed[i]]

    if group:
        groups.append(group)

    return groups


def telegram_escape(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def build_digest_messages(articles: list[Article]) -> list[tuple[str, list[int]]]:
    digest_header = '🗞️ <strong>Altre notizie</strong>'
    messages = []
    text = digest_header
    article_ids = []

    for area, label in DIGEST_AREAS:
        area_articles = [article for article in articles if article.area == area]
        if not area_articles:
            continue

        heading = f'\n\n<strong>{label}</strong>'
        for index, article in enumerate(area_articles):
            line = f'\n• <a href="{telegram_escape(article.link)}">{telegram_escape(article.title)}</a>'
            addition = (heading if index == 0 else '') + line
            if article_ids and len(text) + len(addition) > 4096:
                messages.append((text, article_ids))
                text = digest_header
                article_ids = []
                addition = heading + line
            text += addition
            article_ids.append(article.id)

    if article_ids:
        messages.append((text, article_ids))
    return messages


def send_daily_digest():
    articles = list(
        Article.select()
        .where(Article.is_digest & Article.telegram_message_id.is_null())
        .order_by(Article.published)
    )
    if not articles:
        logger.info('No articles for the daily digest')
        return

    for text, article_ids in build_digest_messages(articles):
        try:
            response = requests.post(
                f'{TELEGRAM_API_URL}/sendMessage',
                json={
                    'chat_id': TELEGRAM_CHANNEL,
                    'text': text,
                    'parse_mode': 'HTML',
                    'disable_web_page_preview': True,
                },
                timeout=10,
            )
            response.raise_for_status()
            message_id = response.json()['result']['message_id']
        except (RequestException, KeyError, TypeError, ValueError):
            logger.exception('Error sending daily digest')
            return

        Article.update(telegram_message_id=message_id).where(Article.id.in_(article_ids)).execute()
        logger.info(f'Sent daily digest message {message_id} with {len(article_ids)} articles')


def clean():
    logger.info('Cleaning old articles')
    # Keep the last 1000 articles
    Article.delete().where(Article.id.not_in(
        Article.select(Article.id).order_by(Article.id.desc()).limit(1000)
    )).execute()

    logger.info('Cleaning old images')
    for filename in os.listdir('images'):
        os.remove(os.path.join('images', filename))


if __name__ == '__main__':
    db.create_tables([Article])
    os.makedirs('images', exist_ok=True)

    clean()
    check()

    scheduler = BlockingScheduler(
        # Run jobs sequentially preventing concurrency issues
        executors={'default': ThreadPoolExecutor(max_workers=1)},
        # Prevent late jobs from being skipped
        job_defaults={'misfire_grace_time': None},
    )

    scheduler.add_job(check, trigger=CronTrigger(minute='*/9'))
    scheduler.add_job(send_daily_digest, trigger=CronTrigger(hour='18', minute='30', timezone='Europe/Rome'))
    scheduler.add_job(clean, trigger=CronTrigger(minute='5', hour='1'))

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
