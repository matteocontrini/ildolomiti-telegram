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
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup
from peewee import SqliteDatabase, Model, TextField, IntegerField
from requests import RequestException
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

BOT_TOKEN = os.environ['BOT_TOKEN']
TELEGRAM_API_URL = f'https://api.telegram.org/bot{BOT_TOKEN}'
TELEGRAM_CHANNEL = os.environ.get('TELEGRAM_CHANNEL', '@ildolomitinews')
TELEGRAM_LOGS_CHANNEL = -1001626800013

DATABASE_PATH = os.environ.get('DATABASE_PATH', 'ildolomiti.db')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%dT%H:%M:%S%z', stream=sys.stdout)
logger = logging.getLogger(__name__)

logger.info('Database path: ' + DATABASE_PATH)

db = SqliteDatabase(DATABASE_PATH)


class Article(Model):
    post_id = IntegerField(null=True)
    title = TextField()
    link = TextField()
    published = IntegerField()
    telegram_message_id = IntegerField(null=True)

    class Meta:
        database = db


@dataclass
class TelegramMessage:
    title: str
    link: str
    tags: list[str]
    description: str
    image: str


def check():
    logger.info('Checking...')

    url = 'https://www.ildolomiti.it/rss.xml?_=' + str(int(time.time()))
    logger.info(f'Fetching {url}')

    resp = requests.get(url, headers={
        'User-Agent': 'Il Dolomiti Telegram (+https://github.com/matteocontrini/ildolomiti-telegram)'
    })

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
        article = Article.get_or_none(link=entry.link)
        if not article:
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
    if '-' in tag:
        tags = tag.split('-')
        tags = [t for t in tags if len(t) > 1]
    else:
        tags = [tag]

    details = fetch_article_details(entry.link)

    message = TelegramMessage(
        title=entry.title.strip(),
        link=entry.link,
        tags=tags + details['tags'],
        description=details['description'] or entry.description,
        image=details['image'] or 'fallback.jpg',
    )

    # Title is different, since we could match the post by post ID but couldn't by link/title
    if details['post_id'] and (article := Article.get_or_none(post_id=details['post_id'])):
        article: Article
        logger.info(f'Updating article: {entry.link} (old: {article.link})')
        if not article.telegram_message_id:
            logger.error('Article has no telegram_message_id, skipping')
        try:
            send_message(message, article.telegram_message_id)
        except RequestException:
            logger.exception('Error updating message')
            return  # so that it's retried later
        send_log(article, entry)
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
            telegram_message_id=message_id
        )


def fetch_article_details(link: str) -> dict:
    resp = requests.get(
        link + '?_=' + str(int(time.time())),  # fix for 404 ending up in the dolomiti cache
        headers={
            'User-Agent': 'Il Dolomiti Telegram (+https://github.com/matteocontrini/ildolomiti-telegram)'
        },
        timeout=10
    )

    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, 'html.parser')

    post_id = None
    description = None
    image = None

    article = soup.select_one('article[id^="node-"]')
    if article:
        post_id = article['id'].split('-')[1]
        description = article.find('div', class_='artSub')
        if description:
            description = description.text.strip()
        else:
            logger.error('Description not found')
        image_url = soup.find('meta', property='og:image')
        if image_url:
            image_url = image_url['content']
            image = download_image(image_url)
        else:
            logger.error('Image meta tag not found')
    else:
        logger.error('Article node not found')

    tags = ['belluno'] if 'section="BELLUNO"' in resp.text else []

    return {
        'post_id': post_id,
        'description': description,
        'image': image,
        'tags': tags,
    }


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

    msg += f'\n\n📰 <a href="{message.link}">Leggi articolo</a>'

    if telegram_message_id:
        payload = {
            'chat_id': TELEGRAM_CHANNEL,
            'message_id': telegram_message_id,
            'caption': msg,
            'parse_mode': 'HTML',
        }
        resp = requests.post(f'{TELEGRAM_API_URL}/editMessageCaption', json=payload)
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
                             })

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


def send_log(article: Article, entry):
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
        })
    except (Exception,):
        logger.exception('Error sending log')


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


def clean():
    logger.info('Cleaning old articles')
    # Keep the last 200 articles
    Article.delete().where(Article.id.not_in(
        Article.select(Article.id).order_by(Article.id.desc()).limit(200)
    )).execute()

    logger.info('Cleaning old images')
    for filename in os.listdir('images'):
        os.remove(os.path.join('images', filename))


if __name__ == '__main__':
    db.create_tables([Article])
    os.makedirs('images', exist_ok=True)

    clean()
    check()

    scheduler = BlockingScheduler()
    scheduler.add_job(check, trigger=CronTrigger(minute='*/9'))
    scheduler.add_job(clean, trigger=CronTrigger(minute='5', hour='1'))

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
